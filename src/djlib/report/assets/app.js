(function () {
  'use strict';

  // MANIFEST is defined by the surrounding index.html.j2 template (parsed
  // from an inline application/json script block) before this file runs.
  // This report performs no persistence beyond a local export button: no
  // fetch/XHR/WebSocket anywhere in this file (design "no HTTP server, no
  // database connection from the browser").
  var manifest = window.MANIFEST;
  var STORAGE_KEY = 'djlib-report-decisions:' + manifest.report_id;
  var ACTIONS = ['CONFIRM', 'CHANGE_PREFERRED', 'REJECT', 'DEFER'];

  var state = {
    decisions: loadDecisions(),
    filterStatus: 'ALL',
    filterConfidence: 'ALL',
    filterFormat: 'ALL',
    filterDecision: 'ALL',
    sortKey: 'confidence',
    sortDir: 'desc',
    unresolvedOnly: false,
    selectedGroupId: null,
    helpOpen: false,
  };

  // -- persistence (browser-local only; see design §23) -------------------

  function loadDecisions() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (err) {
      return {};
    }
  }

  function saveDecisions() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state.decisions));
    } catch (err) {
      // localStorage unavailable (e.g. private browsing) -- decisions still
      // work for the current page session via `state.decisions` in memory.
    }
  }

  function setDecision(groupId, decision, preferredFileId) {
    state.decisions[groupId] = {
      decision: decision,
      preferred_file_id: preferredFileId || null,
      reviewed_at: new Date().toISOString(),
    };
    saveDecisions();
    render();
  }

  function clearDecision(groupId) {
    delete state.decisions[groupId];
    saveDecisions();
    render();
  }

  // -- derived data ---------------------------------------------------------

  function distinctFormats() {
    var formats = {};
    manifest.groups.forEach(function (g) {
      g.files.forEach(function (f) {
        if (f.container_format) formats[f.container_format] = true;
        if (f.extension) formats[f.extension] = true;
      });
    });
    return Object.keys(formats).sort();
  }

  function distinctStatuses() {
    var statuses = {};
    manifest.groups.forEach(function (g) {
      statuses[g.status] = true;
    });
    return Object.keys(statuses).sort();
  }

  function groupDecision(group) {
    return state.decisions[group.group_id] || null;
  }

  function isUnresolved(group) {
    return group.status === 'REVIEW_REQUIRED' && !groupDecision(group);
  }

  function matchesFilters(group) {
    if (state.unresolvedOnly && !isUnresolved(group)) return false;
    if (state.filterStatus !== 'ALL' && group.status !== state.filterStatus) return false;
    if (state.filterConfidence !== 'ALL') {
      var threshold = parseFloat(state.filterConfidence);
      if (group.confidence == null || group.confidence < threshold) return false;
    }
    if (state.filterFormat !== 'ALL') {
      var hasFormat = group.files.some(function (f) {
        return f.container_format === state.filterFormat || f.extension === state.filterFormat;
      });
      if (!hasFormat) return false;
    }
    if (state.filterDecision !== 'ALL') {
      var decision = groupDecision(group);
      if (state.filterDecision === 'UNDECIDED') {
        if (decision) return false;
      } else if (!decision || decision.decision !== state.filterDecision) {
        return false;
      }
    }
    return true;
  }

  function sortValue(group) {
    switch (state.sortKey) {
      case 'confidence':
        return group.confidence == null ? -1 : group.confidence;
      case 'quality_delta':
        return group.quality_delta == null ? -1 : group.quality_delta;
      case 'members':
        return group.file_count;
      case 'path':
        return group.sort_path || '';
      default:
        return 0;
    }
  }

  function visibleGroups() {
    var groups = manifest.groups.filter(matchesFilters);
    var dir = state.sortDir === 'asc' ? 1 : -1;
    groups.sort(function (a, b) {
      var av = sortValue(a);
      var bv = sortValue(b);
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
    return groups;
  }

  function findGroup(groupId) {
    for (var i = 0; i < manifest.groups.length; i++) {
      if (manifest.groups[i].group_id === groupId) return manifest.groups[i];
    }
    return null;
  }

  // -- rendering --------------------------------------------------------

  var el = {};

  function cacheEls() {
    el.groupList = document.getElementById('group-list');
    el.detail = document.getElementById('detail');
    el.filterStatus = document.getElementById('filter-status');
    el.filterConfidence = document.getElementById('filter-confidence');
    el.filterFormat = document.getElementById('filter-format');
    el.filterDecision = document.getElementById('filter-decision');
    el.sortKey = document.getElementById('sort-key');
    el.sortDir = document.getElementById('sort-dir');
    el.unresolvedOnly = document.getElementById('unresolved-only');
    el.prevBtn = document.getElementById('nav-prev');
    el.nextBtn = document.getElementById('nav-next');
    el.exportBtn = document.getElementById('export-btn');
    el.helpBtn = document.getElementById('help-btn');
    el.helpOverlay = document.getElementById('help-overlay');
    el.helpClose = document.getElementById('help-close');
    el.stats = document.getElementById('overview-stats');
    el.reportMeta = document.getElementById('report-meta');
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmtNum(value, digits) {
    if (value == null || isNaN(value)) return '–';
    return Number(value).toFixed(digits == null ? 2 : digits);
  }

  function fmtBytes(bytes) {
    if (bytes == null) return '–';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    var n = bytes;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return n.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
  }

  function fmtDuration(ms) {
    if (ms == null) return '–';
    var totalSeconds = Math.round(ms / 1000);
    var m = Math.floor(totalSeconds / 60);
    var s = totalSeconds % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function identityLine(identity) {
    if (!identity) return '(no active track)';
    var parts = [identity.artist || '?', '—', identity.title || '?'];
    if (identity.version) parts.push('(' + identity.version + ')');
    if (identity.edition) parts.push('[' + identity.edition + ']');
    return parts.join(' ');
  }

  function renderOptions(select, values, current, allLabel) {
    select.innerHTML = '';
    var allOpt = document.createElement('option');
    allOpt.value = 'ALL';
    allOpt.textContent = allLabel;
    select.appendChild(allOpt);
    values.forEach(function (v) {
      var opt = document.createElement('option');
      opt.value = v.value;
      opt.textContent = v.label;
      select.appendChild(opt);
    });
    select.value = current;
  }

  function renderControls() {
    renderOptions(
      el.filterStatus,
      distinctStatuses().map(function (s) { return { value: s, label: s }; }),
      state.filterStatus,
      'All statuses'
    );
    renderOptions(
      el.filterConfidence,
      [
        { value: '0.95', label: '>= 0.95' },
        { value: '0.85', label: '>= 0.85' },
        { value: '0.7', label: '>= 0.70' },
      ],
      state.filterConfidence,
      'Any confidence'
    );
    renderOptions(
      el.filterFormat,
      distinctFormats().map(function (f) { return { value: f, label: f }; }),
      state.filterFormat,
      'All formats'
    );
    renderOptions(
      el.filterDecision,
      ['UNDECIDED'].concat(ACTIONS).map(function (a) { return { value: a, label: a }; }),
      state.filterDecision,
      'Any decision state'
    );
    el.sortKey.value = state.sortKey;
    el.sortDir.value = state.sortDir;
    el.unresolvedOnly.checked = state.unresolvedOnly;
  }

  function renderStats(groups) {
    var total = manifest.groups.length;
    var decided = Object.keys(state.decisions).length;
    el.stats.textContent =
      'showing ' + groups.length + ' / ' + total + ' groups · ' + decided + ' decided this session';
  }

  function renderList() {
    var groups = visibleGroups();
    renderStats(groups);
    el.groupList.innerHTML = '';
    if (!groups.length) {
      var empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No groups match the current filters.';
      el.groupList.appendChild(empty);
      return;
    }
    if (!state.selectedGroupId || !groups.some(function (g) { return g.group_id === state.selectedGroupId; })) {
      state.selectedGroupId = groups[0].group_id;
    }
    groups.forEach(function (group) {
      var row = document.createElement('div');
      row.className = 'group-row' + (group.group_id === state.selectedGroupId ? ' selected' : '');
      row.dataset.groupId = group.group_id;

      var decision = groupDecision(group);
      var top = document.createElement('div');
      top.className = 'row-top';
      top.innerHTML =
        '<span class="badge status-' + group.status + '">' + group.status + '</span>' +
        '<span class="badge decision-' + (decision ? decision.decision : 'none') + '">' +
        (decision ? decision.decision : 'undecided') + '</span>';
      row.appendChild(top);

      var path = document.createElement('div');
      path.className = 'row-path';
      path.textContent = group.sort_path + ' (+' + (group.file_count - 1) + ' more)';
      row.appendChild(path);

      var sub = document.createElement('div');
      sub.className = 'row-top';
      sub.innerHTML =
        '<span>conf ' + fmtNum(group.confidence) + '</span>' +
        '<span>Δq ' + (group.quality_delta == null ? '–' : fmtNum(group.quality_delta, 1)) + '</span>' +
        '<span>' + group.file_count + ' files</span>';
      row.appendChild(sub);

      row.addEventListener('click', function () {
        state.selectedGroupId = group.group_id;
        render();
      });
      el.groupList.appendChild(row);
    });
  }

  function renderDetail() {
    var group = state.selectedGroupId ? findGroup(state.selectedGroupId) : null;
    if (!group || !matchesFilters(group)) {
      var groups = visibleGroups();
      group = groups.length ? groups[0] : null;
    }
    el.detail.innerHTML = '';
    if (!group) {
      var empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No group selected.';
      el.detail.appendChild(empty);
      return;
    }

    var decision = groupDecision(group);

    var header = document.createElement('div');
    header.className = 'detail-header';
    header.innerHTML =
      '<h2>' + escapeHtml(group.group_id) + '</h2>' +
      '<span class="badge status-' + group.status + '">' + group.status + '</span>' +
      '<span class="badge decision-' + (decision ? decision.decision : 'none') + '">' +
      (decision ? decision.decision + (decision.preferred_file_id ? ' → ' + escapeHtml(decision.preferred_file_id) : '') : 'undecided') +
      '</span>';
    el.detail.appendChild(header);

    var reasonsBox = document.createElement('div');
    reasonsBox.className = 'reasons-box';
    reasonsBox.innerHTML =
      '<h3>Why this classification</h3><ul>' +
      group.reasons.map(function (r) { return '<li>' + escapeHtml(r) + '</li>'; }).join('') +
      '</ul>' +
      '<h3>Proposed preferred file: ' + escapeHtml(group.proposed_preferred_file_id || 'none') + '</h3>' +
      '<ul>' +
      group.proposed_preferred_reasons.map(function (r) { return '<li>' + escapeHtml(r) + '</li>'; }).join('') +
      '</ul>';
    el.detail.appendChild(reasonsBox);

    var filesGrid = document.createElement('div');
    filesGrid.className = 'files-grid';
    group.files.forEach(function (file, index) {
      var isPreferred = file.file_id === group.proposed_preferred_file_id;
      var card = document.createElement('div');
      card.className = 'file-card' + (isPreferred ? ' is-preferred' : '');
      var q = file.quality;
      card.innerHTML =
        '<div class="path">[' + (index + 1) + '] ' + escapeHtml(file.relative_path) + '</div>' +
        '<dl>' +
        '<dt>identity</dt><dd>' + escapeHtml(identityLine(file.effective_identity)) + '</dd>' +
        '<dt>format</dt><dd>' + escapeHtml(file.container_format || file.extension) + ' / ' + escapeHtml(file.codec) + '</dd>' +
        '<dt>bitrate</dt><dd>' + (file.bitrate ? Math.round(file.bitrate / 1000) + ' kbps' : '–') + '</dd>' +
        '<dt>sample rate</dt><dd>' + (file.sample_rate || '–') + ' Hz</dd>' +
        '<dt>bit depth</dt><dd>' + (file.bit_depth || '–') + '</dd>' +
        '<dt>duration</dt><dd>' + fmtDuration(file.duration_ms) + '</dd>' +
        '<dt>size</dt><dd>' + fmtBytes(file.size_bytes) + '</dd>' +
        '<dt>metadata completeness</dt><dd>' + fmtNum(file.metadata_completeness) + '</dd>' +
        '<dt>quality score</dt><dd>' + (q ? fmtNum(q.quality_score, 1) : '–') + '</dd>' +
        '<dt>transcode suspicion</dt><dd>' + (q ? q.transcode_suspicion : '–') + '</dd>' +
        '<dt>lossless</dt><dd>' + (q ? q.lossless_status : '–') + '</dd>' +
        '<dt>clipping</dt><dd>' + (q ? q.clipping_status : '–') + '</dd>' +
        '</dl>';
      var makeBtn = document.createElement('button');
      makeBtn.className = 'make-preferred';
      makeBtn.textContent = isPreferred ? 'Proposed preferred' : 'Make preferred (' + (index + 1) + ')';
      makeBtn.addEventListener('click', function () {
        setDecision(group.group_id, 'CHANGE_PREFERRED', file.file_id);
      });
      card.appendChild(makeBtn);
      filesGrid.appendChild(card);
    });
    el.detail.appendChild(filesGrid);

    if (group.pairs.length) {
      var table = document.createElement('table');
      table.className = 'pairs';
      table.innerHTML =
        '<thead><tr><th>pair</th><th>classification</th><th>confidence</th>' +
        '<th>metadata sim</th><th>version</th><th>edition</th><th>chromaprint</th>' +
        '<th>hash equal</th><th>reasons</th></tr></thead>';
      var tbody = document.createElement('tbody');
      group.pairs.forEach(function (pair) {
        var tr = document.createElement('tr');
        tr.innerHTML =
          '<td>' + escapeHtml(pair.left_file_id) + ' / ' + escapeHtml(pair.right_file_id) + '</td>' +
          '<td>' + escapeHtml(pair.classification) + '</td>' +
          '<td>' + fmtNum(pair.confidence) + '</td>' +
          '<td>' + fmtNum(pair.metadata_similarity) + '</td>' +
          '<td>' + escapeHtml(pair.version_compatibility) + '</td>' +
          '<td>' + escapeHtml(pair.edition_compatibility) + '</td>' +
          '<td>' + (pair.chromaprint_similarity == null ? '–' : fmtNum(pair.chromaprint_similarity, 4)) + '</td>' +
          '<td>' + (pair.binary_hash_equal ? 'yes' : 'no') + '</td>' +
          '<td><ul>' + pair.reasons.map(function (r) { return '<li>' + escapeHtml(r) + '</li>'; }).join('') + '</ul></td>';
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      el.detail.appendChild(table);
    }

    var actions = document.createElement('div');
    actions.className = 'actions';
    actions.innerHTML =
      '<button class="confirm' + (decision && decision.decision === 'CONFIRM' ? ' active' : '') + '" data-action="CONFIRM">Confirm (c)</button>' +
      '<button class="reject' + (decision && decision.decision === 'REJECT' ? ' active' : '') + '" data-action="REJECT">Reject (r)</button>' +
      '<button class="defer' + (decision && decision.decision === 'DEFER' ? ' active' : '') + '" data-action="DEFER">Defer (d)</button>' +
      '<button class="clear" data-action="CLEAR">Clear decision</button>';
    actions.querySelectorAll('button[data-action]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var action = btn.dataset.action;
        if (action === 'CLEAR') {
          clearDecision(group.group_id);
        } else {
          setDecision(group.group_id, action, decision ? decision.preferred_file_id : null);
        }
      });
    });
    el.detail.appendChild(actions);
  }

  function render() {
    renderControls();
    renderList();
    renderDetail();
    if (el.reportMeta) {
      el.reportMeta.textContent =
        manifest.report_id + ' · revision ' + manifest.catalog_revision + ' · generated ' + manifest.generated_at;
    }
    // Filter/sort <select>s and the unresolved-only checkbox keep keyboard
    // focus after a change/click even though `renderControls()` just tore
    // down and rebuilt their option lists. Left focused, every subsequent
    // single-letter shortcut (c/r/d/u/e/1-9) would silently do nothing --
    // the keydown handler below deliberately ignores SELECT/INPUT targets
    // so arrow keys and typed letters still work *inside* those controls.
    // Returning focus to the page after each change is what makes the
    // shortcuts keep working afterward, per design §22's own requirement.
    var active = document.activeElement;
    if (active && (active.tagName === 'SELECT' || (active.tagName === 'INPUT' && active.type === 'checkbox'))) {
      active.blur();
    }
  }

  // -- navigation ---------------------------------------------------------

  function selectOffset(offset) {
    var groups = visibleGroups();
    if (!groups.length) return;
    var index = groups.findIndex(function (g) { return g.group_id === state.selectedGroupId; });
    if (index === -1) index = 0;
    var next = (index + offset + groups.length) % groups.length;
    state.selectedGroupId = groups[next].group_id;
    render();
  }

  function currentGroup() {
    return state.selectedGroupId ? findGroup(state.selectedGroupId) : null;
  }

  // -- export ---------------------------------------------------------------

  function exportDecisions() {
    var decisions = Object.keys(state.decisions).map(function (groupId) {
      var d = state.decisions[groupId];
      return {
        group_id: groupId,
        decision: d.decision,
        preferred_file_id: d.preferred_file_id || null,
        reviewed_at: d.reviewed_at,
      };
    });
    var payload = {
      schema_version: 1,
      report_id: manifest.report_id,
      catalog_revision: manifest.catalog_revision,
      generated_at: new Date().toISOString(),
      decisions: decisions,
    };
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'decisions.json';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  // -- wiring ---------------------------------------------------------------

  function wireControls() {
    el.filterStatus.addEventListener('change', function () {
      state.filterStatus = el.filterStatus.value;
      render();
    });
    el.filterConfidence.addEventListener('change', function () {
      state.filterConfidence = el.filterConfidence.value;
      render();
    });
    el.filterFormat.addEventListener('change', function () {
      state.filterFormat = el.filterFormat.value;
      render();
    });
    el.filterDecision.addEventListener('change', function () {
      state.filterDecision = el.filterDecision.value;
      render();
    });
    el.sortKey.addEventListener('change', function () {
      state.sortKey = el.sortKey.value;
      render();
    });
    el.sortDir.addEventListener('change', function () {
      state.sortDir = el.sortDir.value;
      render();
    });
    el.unresolvedOnly.addEventListener('change', function () {
      state.unresolvedOnly = el.unresolvedOnly.checked;
      render();
    });
    el.prevBtn.addEventListener('click', function () { selectOffset(-1); });
    el.nextBtn.addEventListener('click', function () { selectOffset(1); });
    el.exportBtn.addEventListener('click', exportDecisions);
    el.helpBtn.addEventListener('click', function () { el.helpOverlay.classList.remove('hidden'); });
    el.helpClose.addEventListener('click', function () { el.helpOverlay.classList.add('hidden'); });
  }

  function wireKeyboard() {
    document.addEventListener('keydown', function (event) {
      if (event.target && (event.target.tagName === 'SELECT' || event.target.tagName === 'INPUT')) return;
      var key = event.key;
      if (key === 'ArrowDown' || key === 'j') {
        selectOffset(1);
      } else if (key === 'ArrowUp' || key === 'k') {
        selectOffset(-1);
      } else if (key === 'u') {
        state.unresolvedOnly = !state.unresolvedOnly;
        render();
      } else if (key === 'c') {
        var g1 = currentGroup();
        if (g1) setDecision(g1.group_id, 'CONFIRM', groupDecision(g1) ? groupDecision(g1).preferred_file_id : null);
      } else if (key === 'r') {
        var g2 = currentGroup();
        if (g2) setDecision(g2.group_id, 'REJECT', null);
      } else if (key === 'd') {
        var g3 = currentGroup();
        if (g3) setDecision(g3.group_id, 'DEFER', null);
      } else if (key === 'e') {
        exportDecisions();
      } else if (key === '?') {
        el.helpOverlay.classList.toggle('hidden');
      } else if (key === 'Escape') {
        el.helpOverlay.classList.add('hidden');
      } else if (/^[1-9]$/.test(key)) {
        var g4 = currentGroup();
        if (g4) {
          var idx = parseInt(key, 10) - 1;
          if (g4.files[idx]) setDecision(g4.group_id, 'CHANGE_PREFERRED', g4.files[idx].file_id);
        }
      }
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    cacheEls();
    wireControls();
    wireKeyboard();
    render();
  });
})();
