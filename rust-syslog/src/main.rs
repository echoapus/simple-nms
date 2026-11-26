use std::collections::{BTreeMap, HashSet};
use std::env;
use std::fs;
use std::io;
use std::net::UdpSocket;
use std::os::unix::net::UnixDatagram;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const FACILITIES: [&str; 24] = [
    "kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news", "uucp", "cron", "auth",
    "ftp", "ntp", "audit", "alert", "clock", "local0", "local1", "local2", "local3", "local4",
    "local5", "local6", "local7",
];
const SEVERITIES: [&str; 8] = [
    "emerg", "alert", "crit", "err", "warning", "notice", "info", "debug",
];

struct Config {
    host: String,
    port: u16,
    event_socket: String,
    control_socket: String,
    deny_ips: HashSet<String>,
}

struct Event {
    ts: String,
    src_ip: String,
    facility: Option<String>,
    severity: Option<String>,
    varbinds: Option<String>,
    payload: String,
    tags: Option<String>,
}

fn usage() -> ! {
    eprintln!("usage: sieco-syslog --host HOST --port PORT --socket PATH --control-socket PATH [--deny-ip IP]");
    std::process::exit(2);
}

fn config() -> Config {
    let mut args = env::args().skip(1);
    let mut host = None;
    let mut port = None;
    let mut event_socket = None;
    let mut control_socket = None;
    let mut deny_ips = HashSet::new();
    while let Some(arg) = args.next() {
        let value = args.next().unwrap_or_else(|| usage());
        match arg.as_str() {
            "--host" => host = Some(value),
            "--port" => port = value.parse().ok(),
            "--socket" => event_socket = Some(value),
            "--control-socket" => control_socket = Some(value),
            "--deny-ip" => {
                deny_ips.insert(value);
            }
            _ => usage(),
        }
    }
    Config {
        host: host.unwrap_or_else(|| usage()),
        port: port.unwrap_or_else(|| usage()),
        event_socket: event_socket.unwrap_or_else(|| usage()),
        control_socket: control_socket.unwrap_or_else(|| usage()),
        deny_ips,
    }
}

fn json(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len() + 2);
    escaped.push('"');
    for c in value.chars() {
        match c {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            c if c <= '\u{1f}' => escaped.push_str(&format!("\\u{:04x}", c as u32)),
            c => escaped.push(c),
        }
    }
    escaped.push('"');
    escaped
}

fn take_token(input: &str) -> Option<(&str, &str)> {
    let input = input.trim_start_matches(char::is_whitespace);
    let end = input.find(char::is_whitespace).unwrap_or(input.len());
    (!input.is_empty()).then(|| (&input[..end], &input[end..]))
}

fn structured_segment(input: &str) -> Option<(&str, &str)> {
    if input == "-" || input.starts_with("- ") {
        return Some((&input[..1], &input[1..]));
    }
    if !input.starts_with('[') {
        return None;
    }
    let bytes = input.as_bytes();
    let mut end = 0;
    let mut quoted = false;
    let mut escaped = false;
    for (index, byte) in bytes.iter().enumerate() {
        if escaped {
            escaped = false;
            continue;
        }
        if *byte == b'\\' && quoted {
            escaped = true;
        } else if *byte == b'"' {
            quoted = !quoted;
        } else if *byte == b']' && !quoted {
            end = index + 1;
            if bytes.get(end) != Some(&b'[') {
                break;
            }
        }
    }
    (end > 0).then(|| (&input[..end], &input[end..]))
}

fn structured_data(sd: &str) -> Option<String> {
    if sd == "-" {
        return None;
    }
    let mut result: BTreeMap<String, BTreeMap<String, String>> = BTreeMap::new();
    let mut rest = sd;
    while rest.starts_with('[') {
        let bytes = rest.as_bytes();
        let mut end = None;
        let mut quoted = false;
        let mut escaped = false;
        for (index, byte) in bytes.iter().enumerate() {
            if escaped {
                escaped = false;
            } else if *byte == b'\\' && quoted {
                escaped = true;
            } else if *byte == b'"' {
                quoted = !quoted;
            } else if *byte == b']' && !quoted {
                end = Some(index + 1);
                break;
            }
        }
        let end = end?;
        let segment = &rest[..end];
        let content = &segment[1..segment.len() - 1];
        let (id, mut params) = take_token(content)?;
        let mut values = BTreeMap::new();
        loop {
            params = params.trim_start_matches(char::is_whitespace);
            if params.is_empty() {
                break;
            }
            let name_end = params
                .find(|c: char| c.is_whitespace() || c == '=')
                .unwrap_or(params.len());
            let name = &params[..name_end];
            let Some(value_start) = params[name_end..].strip_prefix("=\"") else {
                break;
            };
            let mut value_end = None;
            let mut escaped = false;
            for (i, c) in value_start.char_indices() {
                if escaped {
                    escaped = false;
                } else if c == '\\' {
                    escaped = true;
                } else if c == '"' {
                    value_end = Some(i);
                    break;
                }
            }
            let Some(value_end) = value_end else { break };
            values.insert(name.to_string(), value_start[..value_end].to_string());
            params = &value_start[value_end + 1..];
        }
        result.insert(id.to_string(), values);
        rest = &rest[end..];
    }
    let entries = result
        .into_iter()
        .map(|(id, values)| {
            let values = values
                .into_iter()
                .map(|(key, value)| format!("{}:{}", json(&key), json(&value)))
                .collect::<Vec<_>>()
                .join(",");
            format!("{}:{{{}}}", json(&id), values)
        })
        .collect::<Vec<_>>()
        .join(",");
    Some(format!("{{{entries}}}"))
}

fn leap_year(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap_year(year) => 29,
        2 => 28,
        _ => 0,
    }
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = year - (month <= 2) as i64;
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let mp = month + if month > 2 { -3 } else { 9 };
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let days = days + 719468;
    let era = if days >= 0 { days } else { days - 146096 } / 146097;
    let doe = days - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    year += (month <= 2) as i64;
    (year, month, day)
}

fn format_utc(micros: i128) -> String {
    let seconds = micros.div_euclid(1_000_000);
    let fraction = micros.rem_euclid(1_000_000);
    let days = seconds.div_euclid(86_400);
    let seconds_of_day = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days as i64);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{fraction:06}",
        seconds_of_day / 3600,
        (seconds_of_day % 3600) / 60,
        seconds_of_day % 60,
    )
}

fn parse_timestamp(value: &str) -> Option<String> {
    let bytes = value.as_bytes();
    if bytes.len() < 20
        || bytes.get(4) != Some(&b'-')
        || bytes.get(7) != Some(&b'-')
        || bytes.get(10) != Some(&b'T')
        || bytes.get(13) != Some(&b':')
        || bytes.get(16) != Some(&b':')
    {
        return None;
    }
    let number = |start, end| {
        std::str::from_utf8(&bytes[start..end])
            .ok()?
            .parse::<i64>()
            .ok()
    };
    let year = number(0, 4)?;
    let month = number(5, 7)?;
    let day = number(8, 10)?;
    let hour = number(11, 13)?;
    let minute = number(14, 16)?;
    let second = number(17, 19)?;
    if !(1..=12).contains(&month)
        || !(1..=days_in_month(year, month)).contains(&day)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }
    let mut index = 19;
    let mut micros = 0_i64;
    if bytes.get(index) == Some(&b'.') {
        index += 1;
        let fraction_start = index;
        while bytes.get(index).is_some_and(u8::is_ascii_digit) {
            index += 1;
        }
        if fraction_start == index {
            return None;
        }
        let fraction = &value[fraction_start..index];
        let kept = &fraction[..fraction.len().min(6)];
        micros = kept.parse::<i64>().ok()? * 10_i64.pow((6 - kept.len()) as u32);
    }
    let offset_seconds = match bytes.get(index) {
        Some(b'Z') if index + 1 == bytes.len() => 0,
        Some(b'+') | Some(b'-')
            if index + 6 == bytes.len() && bytes.get(index + 3) == Some(&b':') =>
        {
            let sign = if bytes[index] == b'+' { 1 } else { -1 };
            let offset_hour = number(index + 1, index + 3)?;
            let offset_minute = number(index + 4, index + 6)?;
            if offset_hour > 23 || offset_minute > 59 {
                return None;
            }
            sign * (offset_hour * 3600 + offset_minute * 60)
        }
        _ => return None,
    };
    let unix_seconds =
        days_from_civil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second
            - offset_seconds;
    Some(format_utc(
        unix_seconds as i128 * 1_000_000 + micros as i128,
    ))
}

fn now_utc() -> String {
    let micros = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_micros() as i128;
    format_utc(micros)
}

fn parse(data: &[u8], src_ip: &str) -> Event {
    let text = String::from_utf8_lossy(data)
        .trim_end_matches('\n')
        .to_string();
    let mut event = Event {
        ts: now_utc(),
        src_ip: src_ip.to_string(),
        facility: None,
        severity: None,
        varbinds: None,
        payload: text.clone(),
        tags: None,
    };
    let Some(after_start) = text.strip_prefix('<') else {
        return event;
    };
    let Some(close) = after_start.find('>') else {
        return event;
    };
    if close == 0 || close > 3 || !after_start[..close].bytes().all(|b| b.is_ascii_digit()) {
        return event;
    }
    let Ok(priority) = after_start[..close].parse::<usize>() else {
        return event;
    };
    let message = after_start[close + 1..].trim().to_string();
    let facility = priority >> 3;
    let severity = priority & 7;
    event.facility = Some(FACILITIES.get(facility).unwrap_or(&"").to_string());
    if facility >= FACILITIES.len() {
        event.facility = Some(facility.to_string());
    }
    event.severity = Some(SEVERITIES[severity].to_string());
    event.payload = message.clone();

    let Some((version, mut rest)) = take_token(&message) else {
        return event;
    };
    if !version.bytes().all(|b| b.is_ascii_digit()) {
        return event;
    }
    let Some((timestamp, next)) = take_token(rest) else {
        return event;
    };
    rest = next;
    let Some((hostname, next)) = take_token(rest) else {
        return event;
    };
    rest = next;
    let Some((app_name, next)) = take_token(rest) else {
        return event;
    };
    rest = next;
    let Some((procid, next)) = take_token(rest) else {
        return event;
    };
    rest = next;
    let Some((msgid, next)) = take_token(rest) else {
        return event;
    };
    rest = next;
    let rest = rest.trim_start_matches(char::is_whitespace);
    let Some((sd, message_body)) = structured_segment(rest) else {
        return event;
    };

    if timestamp != "-" {
        if let Some(timestamp) = parse_timestamp(timestamp) {
            event.ts = timestamp;
        }
    }
    event.varbinds = structured_data(sd);
    let mut tags = Vec::new();
    if app_name != "-" {
        tags.push(format!("app:{app_name}"));
    }
    if msgid != "-" {
        tags.push(format!("msgid:{msgid}"));
    }
    if hostname != "-" {
        tags.push(format!("host:{hostname}"));
    }
    if !tags.is_empty() {
        event.tags = Some(tags.join(","));
    }
    let mut prefix = Vec::new();
    if app_name != "-" {
        prefix.push(if procid != "-" {
            format!("{app_name}[{procid}]")
        } else {
            app_name.to_string()
        });
    }
    if msgid != "-" {
        prefix.push(msgid.to_string());
    }
    let prefix = prefix.join(": ");
    let message_body = message_body.trim_start_matches(char::is_whitespace);
    event.payload = if !message_body.is_empty() {
        if prefix.is_empty() {
            message_body.to_string()
        } else {
            format!("{prefix}: {message_body}")
        }
    } else if event.varbinds.is_some() {
        if prefix.is_empty() {
            format!("[Structured Data] {sd}")
        } else {
            format!("{prefix}: [Structured Data] {sd}")
        }
    } else if prefix.is_empty() {
        "(no message)".to_string()
    } else {
        format!("{prefix} (no message)")
    };
    event
}

fn event_json(event: Event, dropped_before: u64) -> String {
    let optional =
        |value: Option<String>| value.map_or_else(|| "null".to_string(), |value| json(&value));
    format!(
        "{{\"ts\":{},\"src_ip\":{},\"type\":\"syslog\",\"facility\":{},\"severity\":{},\"oid\":null,\"varbinds\":{},\"payload\":{},\"tags\":{},\"_bridge_dropped\":{dropped_before}}}",
        json(&event.ts), json(&event.src_ip), optional(event.facility), optional(event.severity),
        optional(event.varbinds), json(&event.payload), optional(event.tags),
    )
}

fn reload_denylist(control: &UnixDatagram, deny_ips: &mut HashSet<String>) {
    let mut buffer = [0_u8; 8192];
    while let Ok(size) = control.recv(&mut buffer) {
        *deny_ips = String::from_utf8_lossy(&buffer[..size])
            .lines()
            .map(str::trim)
            .filter(|ip| !ip.is_empty())
            .map(str::to_string)
            .collect();
    }
}

fn run(config: Config) -> io::Result<()> {
    let input = UdpSocket::bind(format!("{}:{}", config.host, config.port))?;
    input.set_read_timeout(Some(Duration::from_millis(100)))?;
    let _ = fs::remove_file(&config.control_socket);
    let control = UnixDatagram::bind(&config.control_socket)?;
    control.set_nonblocking(true)?;
    let output = UnixDatagram::unbound()?;
    output.connect(&config.event_socket)?;
    output.set_nonblocking(true)?;
    let mut deny_ips = config.deny_ips;
    let mut bridge_dropped = 0_u64;
    let mut buffer = [0_u8; 65_535];
    loop {
        reload_denylist(&control, &mut deny_ips);
        match input.recv_from(&mut buffer) {
            Ok((size, source)) => {
                let source_ip = source.ip().to_string();
                if !deny_ips.contains(&source_ip) {
                    let message = event_json(parse(&buffer[..size], &source_ip), bridge_dropped);
                    match output.send(message.as_bytes()) {
                        Ok(_) => bridge_dropped = 0,
                        Err(ref error) if error.kind() == io::ErrorKind::WouldBlock => {
                            bridge_dropped = bridge_dropped.saturating_add(1);
                        }
                        Err(error) => return Err(error),
                    }
                }
            }
            Err(ref error)
                if error.kind() == io::ErrorKind::WouldBlock
                    || error.kind() == io::ErrorKind::TimedOut => {}
            Err(error) => return Err(error),
        }
    }
}

fn main() {
    if let Err(error) = run(config()) {
        eprintln!("sieco-syslog: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_rfc5424_and_structured_data() {
        let event = parse(
            b"<165>1 2003-10-11T22:14:15.003Z host app 12 ID47 [meta key=\"value\"][other id=\"2\"] hello",
            "192.0.2.1",
        );
        assert_eq!(event.ts, "2003-10-11T22:14:15.003000");
        assert_eq!(event.facility.as_deref(), Some("local4"));
        assert_eq!(event.payload, "app[12]: ID47: hello");
        assert_eq!(
            event.varbinds.as_deref(),
            Some("{\"meta\":{\"key\":\"value\"},\"other\":{\"id\":\"2\"}}")
        );
    }

    #[test]
    fn preserves_rfc3164_and_raw_messages() {
        let syslog = parse(b"<134>router01 BGP: peer down", "192.0.2.1");
        assert_eq!(syslog.facility.as_deref(), Some("local0"));
        assert_eq!(syslog.severity.as_deref(), Some("info"));
        assert_eq!(syslog.payload, "router01 BGP: peer down");

        let raw = parse(b"no PRI field", "192.0.2.1");
        assert!(raw.facility.is_none());
        assert!(raw.severity.is_none());
        assert_eq!(raw.payload, "no PRI field");
    }

    #[test]
    fn includes_dropped_bridge_events_with_the_next_event() {
        let json = event_json(parse(b"<134>message", "192.0.2.1"), 3);
        assert!(json.contains("\"_bridge_dropped\":3"));
    }

    #[test]
    fn normalizes_timestamp_offset() {
        assert_eq!(
            parse_timestamp("2026-09-10T16:00:00+08:00").as_deref(),
            Some("2026-09-10T08:00:00.000000")
        );
    }
}
