/* ================================================================
   Simple NMS — Frontend Controller
   ================================================================ */
(() => {
  'use strict';

  // ── State ──
  const state = {
    page: 1,
    perPage: 50,
    sort: localStorage.getItem('nms_sort') || 'ts',
    order: localStorage.getItem('nms_order') || 'desc',
    q: '',
    types: ['syslog', 'snmptrap', 'webhook'],
    srcIp: '',
    timeFrom: '',
    timeTo: '',
    timeRange: 'all',
    totalEvents: 0,
    totalPages: 1,
    sseConnected: false,
    loading: false,
    currentTab: 'feed',
  };

  // --- Chart instances ---
  let chartTimelineInstance = null;
  let chartTypesInstance = null;
  let chartSeveritiesInstance = null;
  let chartSourcesInstance = null;

  // ── DOM refs ──
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const dom = {
    body: document.body,
    loadingBar: $('#loadingBar'),
    statusDot: $('#statusDot'),
    lastUpdate: $('#lastUpdate'),
    appVersion: $('#appVersion'),
    sseIndicator: $('#sseIndicator'),
    globalSearch: $('#globalSearch'),
    themeToggle: $('#themeToggle'),
    themeIcon: $('#themeIcon'),
    themeLabel: $('#themeLabel'),
    sidebarToggle: $('#sidebarToggle'),
    sidebar: $('#sidebar'),
    sidebarOverlay: $('#sidebarOverlay'),
    timeFrom: $('#timeFrom'),
    timeTo: $('#timeTo'),
    srcIpFilter: $('#srcIpFilter'),
    clearFilters: $('#clearFilters'),
    openCleanupModal: $('#openCleanupModal'),
    kpiTotal: $('#kpiTotal'),
    kpiSyslog: $('#kpiSyslog'),
    kpiSnmptrap: $('#kpiSnmptrap'),
    kpiWebhook: $('#kpiWebhook'),
    countSyslog: $('#countSyslog'),
    countSnmptrap: $('#countSnmptrap'),
    countWebhook: $('#countWebhook'),
    eventsBody: $('#eventsBody'),
    showingCount: $('#showingCount'),
    totalCount: $('#totalCount'),
    pagination: $('#pagination'),
    toast: $('#toast'),
    modalOverlay: $('#modalOverlay'),
    modalMeta: $('#modalMeta'),
    modalContent: $('#modalContent'),
    modalClose: $('#modalClose'),
    modalCopy: $('#modalCopy'),
    cleanupModal: $('#cleanupModal'),
    cleanupClose: $('#cleanupClose'),
    cleanupCancel: $('#cleanupCancel'),
    cleanupConfirm: $('#cleanupConfirm'),
    cleanupDate: $('#cleanupDate'),
    cleanupSummary: $('#cleanupSummary'),
  };

  // ── Helpers ──
  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  function formatNumber(n) {
    if (n == null || isNaN(n)) return '--';
    return n.toLocaleString();
  }

  const LOCAL_TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';

  function parseUtcTimestamp(ts) {
    if (!ts) return null;
    const d = new Date(ts + (ts.endsWith('Z') ? '' : 'Z'));
    return isNaN(d) ? null : d;
  }

  function formatTimestamp(ts) {
    const d = parseUtcTimestamp(ts);
    if (!d) return ts || '';
    const date = d.toLocaleDateString('en-CA');
    const time = d.toLocaleTimeString('en-GB', { hour12: false });
    return `${date} ${time}`;
  }

  function formatUtcTimestamp(ts) {
    const d = parseUtcTimestamp(ts);
    return d ? d.toISOString().replace('T', ' ').replace('Z', ' UTC') : (ts || '');
  }

  function timestampTitle(ts) {
    return `${LOCAL_TIMEZONE} | ${formatUtcTimestamp(ts)}`;
  }

  function toDatetimeLocalValue(isoTs) {
    const d = parseUtcTimestamp(isoTs);
    if (!d) return '';
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function toDateInputValue(d) {
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function escapeHtml(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function truncate(s, len) {
    if (!s) return '';
    return s.length > len ? s.substring(0, len) + '...' : s;
  }

  function showToast(msg) {
    dom.toast.textContent = msg;
    dom.toast.classList.add('show');
    setTimeout(() => dom.toast.classList.remove('show'), 3000);
  }

  function setLoading(on) {
    state.loading = on;
    dom.loadingBar.style.width = on ? '70%' : '0';
    if (!on) setTimeout(() => { dom.loadingBar.style.width = '0'; }, 300);
  }

  function updateLastUpdate() {
    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-GB', { hour12: false });
    dom.lastUpdate.textContent = `Sync: ${timeStr}`;
  }

  // ── API calls ──
  async function loadVersion() {
    try {
      const resp = await fetch('/health');
      const data = await resp.json();
      if (data.version) dom.appVersion.textContent = `v${data.version}`;
    } catch (err) {
      console.error('Failed to fetch version:', err);
    }
  }

  function buildQueryParams() {
    const p = new URLSearchParams();
    p.set('page', state.page);
    p.set('per_page', state.perPage);
    p.set('sort', state.sort);
    p.set('order', state.order);
    if (state.q) p.set('q', state.q);
    if (state.types.length < 3) p.set('type', state.types.join(','));
    if (state.srcIp) p.set('src_ip', state.srcIp);
    if (state.timeFrom) p.set('time_from', state.timeFrom);
    if (state.timeTo) p.set('time_to', state.timeTo);
    return p.toString();
  }

  function buildKpiParams() {
    const p = new URLSearchParams();
    if (state.timeFrom) p.set('time_from', state.timeFrom);
    if (state.timeTo) p.set('time_to', state.timeTo);
    return p.toString();
  }

  async function fetchEvents() {
    setLoading(true);
    try {
      const resp = await fetch(`/api/events?${buildQueryParams()}`);
      const data = await resp.json();
      state.totalEvents = data.total;
      state.totalPages = data.total_pages;
      renderEvents(data.events);
      renderPagination();
      dom.showingCount.textContent = data.events.length;
      dom.totalCount.textContent = formatNumber(data.total);
      updateLastUpdate();
    } catch (err) {
      console.error('Failed to fetch events:', err);
    }
    setLoading(false);
  }

  async function fetchKpi() {
    try {
      const resp = await fetch(`/api/kpi?${buildKpiParams()}`);
      const data = await resp.json();
      animateKpi(dom.kpiTotal, data.total);
      animateKpi(dom.kpiSyslog, data.syslog);
      animateKpi(dom.kpiSnmptrap, data.snmptrap);
      animateKpi(dom.kpiWebhook, data.webhook);
      dom.countSyslog.textContent = formatNumber(data.syslog);
      dom.countSnmptrap.textContent = formatNumber(data.snmptrap);
      dom.countWebhook.textContent = formatNumber(data.webhook);
    } catch (err) {
      console.error('Failed to fetch KPI:', err);
    }
  }

  async function cleanupEvents() {
    const selectedDate = dom.cleanupDate.value;
    if (!selectedDate) {
      showToast('Select a cutoff date first');
      return;
    }

    const cutoff = new Date(`${selectedDate}T00:00:00`);
    if (isNaN(cutoff)) {
      showToast('Invalid cutoff date');
      return;
    }

    dom.cleanupConfirm.disabled = true;
    dom.cleanupConfirm.textContent = 'Deleting...';
    setLoading(true);
    try {
      const resp = await fetch('/api/events/cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ before_ts: cutoff.toISOString().replace('Z', '') }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || 'cleanup failed');

      closeCleanupModal();
      state.page = 1;
      refresh();
      showToast(`Deleted ${formatNumber(data.deleted)} events`);
    } catch (err) {
      console.error('Failed to cleanup events:', err);
      showToast('Failed to delete events');
    } finally {
      dom.cleanupConfirm.disabled = false;
      dom.cleanupConfirm.textContent = 'Delete Events';
      setLoading(false);
    }
  }

  function animateKpi(el, target) {
    const current = parseInt(el.textContent.replace(/,/g, '')) || 0;
    if (current === target) { el.textContent = formatNumber(target); return; }
    const diff = target - current;
    const steps = 15;
    let step = 0;
    const interval = setInterval(() => {
      step++;
      const val = Math.round(current + (diff * (step / steps)));
      el.textContent = formatNumber(val);
      if (step >= steps) { clearInterval(interval); el.textContent = formatNumber(target); }
    }, 20);
  }

  // ── Rendering ──
  // Store current events for modal access
  let currentEvents = [];

  function renderEvents(events) {
    currentEvents = events;
    if (!events.length) {
      dom.eventsBody.innerHTML = `
        <tr><td colspan="5">
          <div class="empty-state">
            <div class="icon">&#128225;</div>
            <p>No events found matching your filters.</p>
          </div>
        </td></tr>`;
      return;
    }
    dom.eventsBody.innerHTML = events.map((e, idx) => {
      const payload = truncate(e.payload_preview || e.payload || e.oid || '', 100);
      const severityClass = e.severity || '';
      return `<tr>
        <td class="col-ts" title="${escapeHtml(timestampTitle(e.ts))}">${escapeHtml(formatTimestamp(e.ts))}</td>
        <td class="col-ip">${escapeHtml(e.src_ip || '')}</td>
        <td class="col-type"><span class="type-badge ${e.type}">${escapeHtml(e.type)}</span></td>
        <td class="col-severity">${e.severity ? `<span class="severity-dot ${severityClass}"></span>${escapeHtml(e.severity)}` : '<span style="color:var(--text-muted)">—</span>'}</td>
        <td class="col-payload" onclick="NMS.showDetail(${idx})">${escapeHtml(payload)}</td>
      </tr>`;
    }).join('');
  }

  function showDetailModal(idx) {
    const e = currentEvents[idx];
    if (!e) return;

    // Build meta info
    dom.modalMeta.innerHTML = [
      { label: `Time (${LOCAL_TIMEZONE})`, value: formatTimestamp(e.ts) },
      { label: 'UTC Time', value: formatUtcTimestamp(e.ts) },
      { label: 'Source', value: e.src_ip || '—' },
      { label: 'Type', value: e.type },
      { label: 'Facility', value: e.facility || '—' },
      { label: 'Severity', value: e.severity || '—' },
      { label: 'OID', value: e.oid || '—' },
    ].map(m => `<div class="modal-meta-item"><span class="modal-meta-label">${m.label}</span><span class="modal-meta-value">${escapeHtml(m.value)}</span></div>`).join('');

    // Format payload — try to pretty-print JSON
    let content = e.payload || e.varbinds || '(no content)';
    try {
      const parsed = JSON.parse(content);
      content = JSON.stringify(parsed, null, 2);
    } catch (_) {}
    dom.modalContent.textContent = content;

    dom.modalOverlay.classList.add('open');
  }

  function closeDetailModal() {
    dom.modalOverlay.classList.remove('open');
  }

  function openCleanupModal() {
    dom.cleanupDate.value = toDateInputValue(new Date());
    updateCleanupSummary();
    dom.cleanupModal.classList.add('open');
  }

  function closeCleanupModal() {
    dom.cleanupModal.classList.remove('open');
  }

  function updateCleanupSummary() {
    const selectedDate = dom.cleanupDate.value;
    if (!selectedDate) {
      dom.cleanupSummary.textContent = 'Select a date. Events before that date will be permanently deleted.';
      return;
    }
    const cutoff = new Date(`${selectedDate}T00:00:00`);
    dom.cleanupSummary.textContent =
      `This permanently deletes events before ${cutoff.toLocaleString()} (${LOCAL_TIMEZONE}).`;
  }

  function renderPagination() {
    const { page, totalPages } = state;
    if (totalPages <= 1) { dom.pagination.innerHTML = ''; return; }

    let html = '';
    html += `<button class="page-btn" onclick="NMS.goPage(1)" ${page <= 1 ? 'disabled' : ''}>&#171;</button>`;
    html += `<button class="page-btn" onclick="NMS.goPage(${page - 1})" ${page <= 1 ? 'disabled' : ''}>&#8249;</button>`;

    // Show page numbers
    let start = Math.max(1, page - 2);
    let end = Math.min(totalPages, page + 2);
    if (start > 1) html += `<span class="page-info">...</span>`;
    for (let i = start; i <= end; i++) {
      html += `<button class="page-btn ${i === page ? 'active' : ''}" onclick="NMS.goPage(${i})">${i}</button>`;
    }
    if (end < totalPages) html += `<span class="page-info">...</span>`;

    html += `<button class="page-btn" onclick="NMS.goPage(${page + 1})" ${page >= totalPages ? 'disabled' : ''}>&#8250;</button>`;
    html += `<button class="page-btn" onclick="NMS.goPage(${totalPages})" ${page >= totalPages ? 'disabled' : ''}>&#187;</button>`;

    dom.pagination.innerHTML = html;
  }

  // ── Sort ──
  function handleSort(col) {
    if (state.sort === col) {
      state.order = state.order === 'desc' ? 'asc' : 'desc';
    } else {
      state.sort = col;
      state.order = col === 'ts' ? 'desc' : 'asc';
    }
    localStorage.setItem('nms_sort', state.sort);
    localStorage.setItem('nms_order', state.order);
    state.page = 1;
    updateSortUI();
    fetchEvents();
  }

  function updateSortUI() {
    $$('thead th[data-sort]').forEach(th => {
      const col = th.dataset.sort;
      const arrow = th.querySelector('.sort-arrow');
      if (col === state.sort) {
        th.classList.add('sorted');
        arrow.innerHTML = state.order === 'desc' ? '&#9660;' : '&#9650;';
      } else {
        th.classList.remove('sorted');
        arrow.innerHTML = '';
      }
    });
  }

  // ── Time range ──
  function setTimeRange(range) {
    state.timeRange = range;
    $$('.time-btn').forEach(b => b.classList.toggle('active', b.dataset.range === range));

    const now = new Date();
    let from = '';
    let to = '';

    switch (range) {
      case '5m':
        from = new Date(now.getTime() - 5 * 60000).toISOString();
        break;
      case '1h':
        from = new Date(now.getTime() - 3600000).toISOString();
        break;
      case 'today':
        from = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
        break;
      case 'all':
        from = '';
        to = '';
        break;
      case 'custom':
        return; // Use the date inputs directly
    }

    state.timeFrom = from;
    state.timeTo = to;
    dom.timeFrom.value = toDatetimeLocalValue(from);
    dom.timeTo.value = toDatetimeLocalValue(to);
    state.page = 1;
    refresh();
  }

  // ── SSE ──
  let eventSource = null;
  let sseEventCount = 0;

  function connectSSE() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource('/api/sse');

    eventSource.onopen = () => {
      state.sseConnected = true;
      dom.statusDot.classList.remove('disconnected');
      dom.sseIndicator.textContent = 'SSE: connected';
    };

    eventSource.onmessage = (e) => {
      sseEventCount++;
      dom.sseIndicator.textContent = `SSE: ${sseEventCount}`;
      // Refresh data on new events (debounced)
      debouncedRefresh();
    };

    eventSource.onerror = () => {
      state.sseConnected = false;
      dom.statusDot.classList.add('disconnected');
      dom.sseIndicator.textContent = 'SSE: reconnecting...';
    };
  }

  const debouncedRefresh = debounce(() => {
    fetchEvents();
    fetchKpi();
    updateLastUpdate();
  }, 500);

  // ── Theme ──
  const systemTheme = matchMedia('(prefers-color-scheme: dark)');

  function setTheme(isDark) {
    dom.body.classList.toggle('dark', isDark);
    document.documentElement.dataset.theme = isDark ? 'dark' : 'light';
    updateThemeUI();
  }

  function initTheme() {
    const saved = localStorage.getItem('nms_theme');
    setTheme(saved ? saved === 'dark' : systemTheme.matches);
    systemTheme.addEventListener('change', event => {
      if (!localStorage.getItem('nms_theme')) setTheme(event.matches);
    });
  }

  function toggleTheme() {
    const isDark = !dom.body.classList.contains('dark');
    localStorage.setItem('nms_theme', isDark ? 'dark' : 'light');
    setTheme(isDark);
  }

  function updateThemeUI() {
    const isDark = dom.body.classList.contains('dark');
    dom.themeIcon.innerHTML = isDark ? '&#9788;' : '&#9790;';
    dom.themeLabel.textContent = isDark ? 'Light' : 'Dark';
    dom.themeToggle.setAttribute('aria-label', `Switch to ${isDark ? 'light' : 'dark'} theme`);
  }

  // ── Sidebar (mobile) ──
  function toggleSidebar() {
    dom.sidebar.classList.toggle('open');
    dom.sidebarOverlay.classList.toggle('open');
  }

  function closeSidebar() {
    dom.sidebar.classList.remove('open');
    dom.sidebarOverlay.classList.remove('open');
  }

  // ── Clear filters ──
  function clearFilters() {
    state.q = '';
    state.srcIp = '';
    state.types = ['syslog', 'snmptrap', 'webhook'];
    state.page = 1;
    dom.globalSearch.value = '';
    dom.srcIpFilter.value = '';
    $$('#typeFilters input[type="checkbox"]').forEach(cb => { cb.checked = true; });
    setTimeRange('all');
  }

  // ── Refresh all ──
  function refresh() {
    fetchKpi();
    if (state.currentTab === 'feed') {
      fetchEvents();
    } else {
      fetchAnalytics();
    }
  }

  // ── Analytics API & Rendering ──
  async function fetchAnalytics() {
    setLoading(true);
    try {
      const resp = await fetch(`/api/analytics?${buildQueryParams()}`);
      const data = await resp.json();
      renderCharts(data);
    } catch (err) {
      console.error('Failed to fetch analytics:', err);
    }
    setLoading(false);
  }

  function getThemeColors() {
    const style = getComputedStyle(document.body);
    const isDark = dom.body.classList.contains('dark');
    return {
      text: isDark ? '#e2e8f0' : '#1e293b',
      textMuted: isDark ? '#64748b' : '#94a3b8',
      border: isDark ? '#2d3548' : '#cbd5e1',
      accent: isDark ? '#60a5fa' : '#3b82f6',
      syslog: '#3b82f6',
      snmptrap: '#f59e0b',
      webhook: '#10b981',
      total: '#6366f1',
    };
  }

  function formatTimelineLabel(timeStr, scale) {
    if (!timeStr) return '';
    try {
      const d = parseUtcTimestamp(timeStr.replace(' ', 'T'));
      if (scale === 'day') {
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
      } else {
        return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
      }
    } catch (_) {
      return timeStr;
    }
  }

  function renderCharts(data) {
    const colors = getThemeColors();

    // 1. Timeline Chart (Line)
    const ctxTimeline = document.getElementById('chartTimeline').getContext('2d');
    if (chartTimelineInstance) chartTimelineInstance.destroy();
    
    const timelineLabels = data.timeline.map(x => formatTimelineLabel(x.time, data.timeline_scale));
    const timelineValues = data.timeline.map(x => x.count);

    chartTimelineInstance = new Chart(ctxTimeline, {
      type: 'line',
      data: {
        labels: timelineLabels,
        datasets: [{
          label: 'Events Count',
          data: timelineValues,
          borderColor: colors.accent,
          backgroundColor: 'rgba(59, 130, 246, 0.1)',
          fill: true,
          tension: 0.3,
          borderWidth: 2
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            mode: 'index',
            intersect: false
          }
        },
        scales: {
          x: {
            grid: { color: colors.border },
            ticks: { color: colors.textMuted }
          },
          y: {
            grid: { color: colors.border },
            ticks: { color: colors.textMuted, stepSize: 1 },
            beginAtZero: true
          }
        }
      }
    });

    // 2. Event Types Chart (Doughnut)
    const ctxTypes = document.getElementById('chartTypes').getContext('2d');
    if (chartTypesInstance) chartTypesInstance.destroy();

    const typeLabels = ['Syslog', 'SNMP Trap', 'Webhook'];
    const typeValues = [data.types.syslog || 0, data.types.snmptrap || 0, data.types.webhook || 0];

    chartTypesInstance = new Chart(ctxTypes, {
      type: 'doughnut',
      data: {
        labels: typeLabels,
        datasets: [{
          data: typeValues,
          backgroundColor: [colors.syslog, colors.snmptrap, colors.webhook],
          borderWidth: 0
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            position: 'bottom',
            labels: { color: colors.text }
          }
        }
      }
    });

    // 3. Severities Chart (Bar)
    const ctxSeverities = document.getElementById('chartSeverities').getContext('2d');
    if (chartSeveritiesInstance) chartSeveritiesInstance.destroy();

    const sevMap = {
      'emerg': 'Emergency',
      'alert': 'Alert',
      'crit': 'Critical',
      'err': 'Error',
      'warning': 'Warning',
      'notice': 'Notice',
      'info': 'Info',
      'debug': 'Debug',
      'none': 'None / Webhook / Trap'
    };

    const sevColorMap = {
      'emerg': '#991b1b',
      'alert': '#dc2626',
      'crit': '#ef4444',
      'err': '#f97316',
      'warning': '#eab308',
      'notice': '#3b82f6',
      'info': '#22c55e',
      'debug': '#94a3b8',
      'none': '#64748b'
    };

    const sevLabels = [];
    const sevValues = [];
    const sevColors = [];

    for (const [key, label] of Object.entries(sevMap)) {
      if (data.severities[key] !== undefined) {
        sevLabels.push(label);
        sevValues.push(data.severities[key]);
        sevColors.push(sevColorMap[key]);
      }
    }

    chartSeveritiesInstance = new Chart(ctxSeverities, {
      type: 'bar',
      data: {
        labels: sevLabels,
        datasets: [{
          data: sevValues,
          backgroundColor: sevColors,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
        plugins: {
          legend: { display: false }
        },
        scales: {
          x: {
            grid: { color: colors.border },
            ticks: { color: colors.textMuted, stepSize: 1 },
            beginAtZero: true
          },
          y: {
            grid: { display: false },
            ticks: { color: colors.text }
          }
        }
      }
    });

    // 4. Top Sources Chart (Bar)
    const ctxSources = document.getElementById('chartSources').getContext('2d');
    if (chartSourcesInstance) chartSourcesInstance.destroy();

    const sourceLabels = data.top_ips.map(x => x.ip);
    const sourceValues = data.top_ips.map(x => x.count);

    chartSourcesInstance = new Chart(ctxSources, {
      type: 'bar',
      data: {
        labels: sourceLabels,
        datasets: [{
          data: sourceValues,
          backgroundColor: colors.accent,
          borderRadius: 4
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false }
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: colors.textMuted }
          },
          y: {
            grid: { color: colors.border },
            ticks: { color: colors.textMuted, stepSize: 1 },
            beginAtZero: true
          }
        }
      }
    });
  }

  // ── Switch View Tabs ──
  function switchTab(tab) {
    state.currentTab = tab;
    $$('.tab-btn').forEach(b => b.classList.remove('active'));
    $$('.tab-content').forEach(c => c.classList.remove('active'));

    if (tab === 'feed') {
      $('#tabFeed').classList.add('active');
      $('#viewFeed').classList.add('active');
      fetchEvents();
    } else if (tab === 'analytics') {
      $('#tabAnalytics').classList.add('active');
      $('#viewAnalytics').classList.add('active');
      fetchAnalytics();
    } else if (tab === 'settings') {
      $('#tabSettings').classList.add('active');
      $('#viewSettings').classList.add('active');
      fetchSettings();
    }
  }

  // ── Settings & MIBs ──
  async function fetchSettings() {
    try {
      const respConf = await fetch('/api/config');
      if (respConf.ok) {
        const dataConf = await respConf.json();
        $('#inputCommunity').value = dataConf.community || 'simplenms';
        $('#inputWebhookPort').value = dataConf.webhook_port || 80;
        $('#inputSyslogDenyIps').value = (dataConf.syslog_deny_ips || []).join('\n');
        const tls = dataConf.syslog_tls || {};
        $('#inputSyslogTlsEnabled').checked = !!tls.enabled;
        $('#inputSyslogTlsHost').value = tls.host || '0.0.0.0';
        $('#inputSyslogTlsPort').value = tls.port || 6514;
        $('#inputSyslogTlsClientCert').checked = !!tls.require_client_cert;
        $('#syslogTlsCertStatus').textContent = tls.cert_configured ? 'Configured' : 'Missing';
        $('#syslogTlsKeyStatus').textContent = tls.key_configured ? 'Configured' : 'Missing';
        $('#syslogTlsCaStatus').textContent = tls.ca_configured ? 'Configured' : 'Optional';
        const ready = tls.cert_configured && tls.key_configured && (!tls.require_client_cert || tls.ca_configured);
        const status = $('#syslogTlsOverallStatus');
        status.textContent = tls.enabled ? (ready ? 'Live' : 'Needs files') : (ready ? 'Ready to enable' : 'Disabled');
        status.classList.toggle('warn', tls.enabled && !ready);
      }
      await fetchMibs();
    } catch (err) {
      console.error('Failed to fetch settings:', err);
    }
  }

  async function fetchMibs() {
    try {
      const resp = await fetch('/api/mibs');
      const mibs = await resp.json();
      renderMibs(mibs);
    } catch (err) {
      console.error('Failed to fetch MIBs:', err);
      $('#mibListBody').innerHTML = `<tr><td colspan="4" style="padding: 20px; text-align: center; color: var(--text-muted);">Failed to load MIBs</td></tr>`;
    }
  }

  function renderMibs(mibs) {
    const tbody = $('#mibListBody');
    if (!mibs || mibs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" style="padding: 20px; text-align: center; color: var(--text-muted);">No custom MIBs uploaded yet</td></tr>`;
      return;
    }

    tbody.innerHTML = mibs.map(mib => {
      const sizeKB = (mib.size / 1024).toFixed(2);
      return `
        <tr style="border-bottom: 1px solid var(--border);">
          <td style="padding: 12px 16px; font-family: var(--font-mono); font-size: 0.85rem;">${mib.filename}</td>
          <td style="padding: 12px 16px; font-weight: 500;">${mib.module_name}</td>
          <td style="padding: 12px 16px; color: var(--text-muted);">${sizeKB} KB</td>
          <td style="padding: 12px 16px; text-align: right;">
            <button class="btn" style="padding: 6px 12px; font-size: 0.8rem; border: 1px solid #ff4d4d; color: #ff4d4d; background: transparent; border-radius: 4px; cursor: pointer;" onclick="NMS.deleteMib('${mib.filename}')">Delete</button>
          </td>
        </tr>
      `;
    }).join('');
  }

  async function saveServiceConfig() {
    const community = $('#inputCommunity').value.trim();
    const webhookPortStr = $('#inputWebhookPort').value.trim();
    
    if (!community) {
      showToast('Community string cannot be empty');
      return;
    }
    
    const port = parseInt(webhookPortStr, 10);
    if (isNaN(port) || port < 1 || port > 65535) {
      showToast('Port must be a valid number between 1 and 65535');
      return;
    }
    const btn = $('#btnSaveService');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
      const resp = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          community: community,
          webhook_port: port
        })
      });
      if (resp.ok) {
        const data = await resp.json();
        if (data.port_changed) {
          showToast('Config saved. Restart service to apply new port');
          alert('Configuration saved successfully.\n\nNOTE: You have changed the web server port. You must restart the service in terminal to apply the new port:\n\nsudo systemctl restart simple-nms');
        } else {
          showToast('Service settings saved');
        }
      } else {
        const err = await resp.json();
        showToast(`Failed: ${err.error || 'Unknown error'}`);
      }
    } catch (err) {
      showToast('Failed to save configuration');
    }
    btn.disabled = false;
    btn.textContent = 'Save service settings';
  }

  async function saveDenyIps() {
    const denyIps = $('#inputSyslogDenyIps').value.split('\n').map(s => s.trim()).filter(Boolean);
    const btn = $('#btnSaveDenyIps');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
      const resp = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ syslog_deny_ips: denyIps })
      });
      if (resp.ok) {
        showToast('Denylist saved');
      } else {
        const err = await resp.json();
        showToast(`Failed: ${err.error || 'Unknown error'}`);
      }
    } catch (err) {
      showToast('Failed to save denylist');
    }
    btn.disabled = false;
    btn.textContent = 'Save denylist';
  }

  async function applyTlsConfig() {
    const host = $('#inputSyslogTlsHost').value.trim();
    const port = parseInt($('#inputSyslogTlsPort').value, 10);
    const enabled = $('#inputSyslogTlsEnabled').checked;
    if (!host || isNaN(port) || port < 1 || port > 65535) return showToast('TLS host and port must be valid');
    if (enabled && !confirm('Applying TLS changes disconnects current TLS Syslog clients. Continue?')) return;
    const btn = $('#btnApplyTls');
    btn.disabled = true; btn.textContent = 'Applying…';
    try {
      const resp = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
        syslog_tls: { enabled, host, port, require_client_cert: $('#inputSyslogTlsClientCert').checked, reload: true }
      })});
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || 'TLS reload failed');
      showToast(enabled ? 'TLS listener reloaded' : 'TLS listener disabled');
      fetchSettings();
    } catch (err) { showToast(`TLS changes not applied: ${err.message}`); }
    btn.disabled = false; btn.textContent = 'Apply TLS changes';
  }

  async function deleteMib(filename) {
    if (!confirm(`Are you sure you want to delete MIB file ${filename}?`)) return;
    try {
      const resp = await fetch(`/api/mibs/${filename}`, { method: 'DELETE' });
      if (resp.ok) {
        showToast('MIB file deleted successfully');
        fetchMibs();
      } else {
        const err = await resp.json();
        showToast(`Failed: ${err.error}`);
      }
    } catch (err) {
      showToast('Failed to delete MIB file');
    }
  }

  function initMibUpload() {
    const fileInput = $('#mibFileInput');
    const selectBtn = $('#btnSelectMibFile');
    const uploadBtn = $('#btnUploadMib');
    const nameLabel = $('#selectedMibFileName');
    const statusMsg = $('#uploadStatusMsg');

    if (!selectBtn || !fileInput || !uploadBtn) return;

    selectBtn.addEventListener('click', () => fileInput.click());
    
    fileInput.addEventListener('change', () => {
      const file = fileInput.files[0];
      if (file) {
        nameLabel.textContent = file.name;
        uploadBtn.disabled = false;
      } else {
        nameLabel.textContent = 'No file chosen';
        uploadBtn.disabled = true;
      }
      statusMsg.style.display = 'none';
    });

    uploadBtn.addEventListener('click', async () => {
      const file = fileInput.files[0];
      if (!file) return;

      uploadBtn.disabled = true;
      uploadBtn.textContent = 'Uploading...';
      statusMsg.style.display = 'block';
      statusMsg.style.color = 'var(--text-muted)';
      statusMsg.textContent = 'Validating and uploading MIB file...';

      const formData = new FormData();
      formData.append('file', file);

      try {
        const resp = await fetch('/api/mibs', {
          method: 'POST',
          body: formData
        });
        const data = await resp.json();
        if (resp.ok) {
          statusMsg.style.color = '#2eb82e';
          statusMsg.textContent = data.loaded ? `Success: Loaded module ${data.module_name}` : `Saved module ${data.module_name}; SNMP collector is not running`;
          showToast(data.loaded ? 'MIB uploaded and compiled successfully' : 'MIB saved');
          fileInput.value = '';
          nameLabel.textContent = 'No file chosen';
          fetchMibs();
        } else {
          statusMsg.style.color = '#ff4d4d';
          statusMsg.textContent = `Validation Error: ${data.error}`;
          showToast('MIB upload failed validation');
        }
      } catch (err) {
        statusMsg.style.color = '#ff4d4d';
        statusMsg.textContent = 'Network error during upload';
        showToast('Network error');
      }
      uploadBtn.disabled = fileInput.files.length === 0;
      uploadBtn.textContent = 'Upload & Validate';
    });
  }

  function initTlsUploads() {
    $$('[data-tls-upload]').forEach(button => button.addEventListener('click', async () => {
      const kind = button.dataset.tlsUpload;
      const input = $(`#syslogTls${kind[0].toUpperCase()}${kind.slice(1)}`);
      const file = input.files[0];
      if (!file) return showToast('Choose a file first');
      const form = new FormData();
      form.append('kind', kind);
      form.append('file', file);
      button.disabled = true;
      try {
        const response = await fetch('/api/syslog-tls/certificates', { method: 'POST', body: form });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Upload failed');
        showToast('TLS file uploaded; save configuration to reload the listener');
        fetchSettings();
      } catch (err) {
        showToast(`TLS upload failed: ${err.message}`);
      }
      button.disabled = false;
    }));
  }

  // ── Event listeners ──
  function bindEvents() {
    // Global search (300ms debounce)
    dom.globalSearch.addEventListener('input', debounce(() => {
      state.q = dom.globalSearch.value.trim();
      state.page = 1;
      refresh();
    }, 300));

    // Theme
    dom.themeToggle.addEventListener('click', toggleTheme);

    // Sidebar
    dom.sidebarToggle.addEventListener('click', toggleSidebar);
    dom.sidebarOverlay.addEventListener('click', closeSidebar);

    // Time range buttons
    $$('.time-btn').forEach(btn => {
      btn.addEventListener('click', () => setTimeRange(btn.dataset.range));
    });

    // Custom date inputs
    dom.timeFrom.addEventListener('change', () => {
      state.timeFrom = dom.timeFrom.value ? new Date(dom.timeFrom.value).toISOString() : '';
      state.timeRange = 'custom';
      $$('.time-btn').forEach(b => b.classList.remove('active'));
      state.page = 1;
      refresh();
    });

    dom.timeTo.addEventListener('change', () => {
      state.timeTo = dom.timeTo.value ? new Date(dom.timeTo.value).toISOString() : '';
      state.timeRange = 'custom';
      $$('.time-btn').forEach(b => b.classList.remove('active'));
      state.page = 1;
      refresh();
    });

    // Type checkboxes
    $$('#typeFilters input[type="checkbox"]').forEach(cb => {
      cb.addEventListener('change', () => {
        state.types = [...$$('#typeFilters input[type="checkbox"]:checked')].map(c => c.value);
        if (state.types.length === 0) state.types = ['syslog', 'snmptrap', 'webhook'];
        state.page = 1;
        refresh();
      });
    });

    // IP filter (debounced)
    dom.srcIpFilter.addEventListener('input', debounce(() => {
      state.srcIp = dom.srcIpFilter.value.trim();
      state.page = 1;
      refresh();
    }, 300));

    // Sort headers
    $$('thead th[data-sort]').forEach(th => {
      th.addEventListener('click', () => handleSort(th.dataset.sort));
    });

    // Clear filters
    dom.clearFilters.addEventListener('click', clearFilters);

    // Cleanup modal
    dom.openCleanupModal.addEventListener('click', openCleanupModal);
    dom.cleanupClose.addEventListener('click', closeCleanupModal);
    dom.cleanupCancel.addEventListener('click', closeCleanupModal);
    dom.cleanupDate.addEventListener('change', updateCleanupSummary);
    dom.cleanupConfirm.addEventListener('click', cleanupEvents);
    dom.cleanupModal.addEventListener('click', (e) => {
      if (e.target === dom.cleanupModal) closeCleanupModal();
    });

    // Modal
    dom.modalClose.addEventListener('click', closeDetailModal);
    dom.modalOverlay.addEventListener('click', (e) => {
      if (e.target === dom.modalOverlay) closeDetailModal();
    });
    dom.modalCopy.addEventListener('click', () => {
      navigator.clipboard.writeText(dom.modalContent.textContent).then(() => {
        dom.modalCopy.textContent = 'Copied!';
        setTimeout(() => { dom.modalCopy.textContent = 'Copy'; }, 1500);
      });
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeDetailModal();
        closeCleanupModal();
      }
    });

    // Settings community save button
    $('#btnSaveService')?.addEventListener('click', saveServiceConfig);
    $('#btnSaveDenyIps')?.addEventListener('click', saveDenyIps);
    $('#btnApplyTls')?.addEventListener('click', applyTlsConfig);
  }

  // ── Public API for pagination buttons ──
  window.NMS = {
    goPage(p) {
      state.page = Math.max(1, Math.min(p, state.totalPages));
      fetchEvents();
    },
    showDetail(idx) {
      showDetailModal(idx);
    },
    switchTab(tab) {
      switchTab(tab);
    },
    deleteMib(filename) {
      deleteMib(filename);
    }
  };

  // ── Init ──
  function init() {
    initTheme();
    updateSortUI();
    bindEvents();
    initMibUpload();
    initTlsUploads();
    loadVersion();
    refresh();
    connectSSE();

    // Periodic refresh as fallback (every 30s)
    setInterval(() => {
      if (!state.loading) refresh();
    }, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
