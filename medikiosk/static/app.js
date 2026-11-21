/* MediKiosk front-end helpers: live queue, live token updates, tablet alerts. */
var MK = (function () {
  var csrf = document.querySelector('meta[name="csrf-token"]').content;
  var loggedIn = document.body.dataset.loggedIn === '1';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function toast(html, cls) {
    var box = document.getElementById('toasts');
    var el = document.createElement('div');
    el.className = 'toast ' + (cls || '');
    el.innerHTML = html + '<button class="toast-x" aria-label="Close">&times;</button>';
    el.querySelector('.toast-x').onclick = function () { el.remove(); };
    box.appendChild(el);
    return el;
  }

  /* ---------- Live queue board ---------- */
  function statusClass(s) { return s.toLowerCase().replace(/\s/g, ''); }

  function renderQueue(data) {
    document.getElementById('q-time').textContent = data.time;
    var html = data.doctors.map(function (d) {
      var tokens = d.waiting_tokens.length
        ? d.waiting_tokens.map(function (t) { return '<span class="chip">#' + t + '</span>'; }).join('')
        : '<span class="muted small">No one waiting</span>';
      return '<div class="card q-card q-' + statusClass(d.status) + '">' +
        '<div class="row-between"><h3>' + esc(d.name) + '</h3><span class="badge badge-' + statusClass(d.status) + '">' + esc(d.status) + '</span></div>' +
        '<p class="muted small">' + esc(d.ward) + ' · Room ' + esc(d.room) + ' · ' + esc(d.shift) + '</p>' +
        '<div class="q-now"><small>Now serving</small><b>' + (d.now_serving ? '#' + d.now_serving : '–') + '</b>' +
        (d.busy_until ? '<span class="muted small">until ' + esc(d.busy_until) + '</span>' : '') + '</div>' +
        '<p class="small"><b>' + d.waiting_count + '</b> waiting · next free slot <b>' + esc(d.next_free) + '</b>' +
        (d.est_wait_min != null ? ' (~' + d.est_wait_min + ' min)' : '') + '</p>' +
        '<div class="chips">' + tokens + '</div></div>';
    }).join('');
    document.getElementById('queue-board').innerHTML = html;
  }

  function startQueueBoard() {
    function load() {
      fetch('/api/queue').then(function (r) { return r.json(); }).then(renderQueue).catch(function () {});
    }
    load();
    setInterval(load, 10000);
  }

  /* ---------- Live token box (dashboard / appointment page) ---------- */
  function startLiveAppointments() {
    document.querySelectorAll('[data-live-appt]').forEach(function (box) {
      var id = box.dataset.liveAppt;
      function load() {
        fetch('/api/appointments/' + id).then(function (r) { return r.json(); }).then(function (d) {
          var set = function (f, v) { var el = box.querySelector('[data-field="' + f + '"]'); if (el) el.textContent = v; };
          set('state', d.state);
          set('ahead', d.patients_ahead);
          set('wait', d.wait_min == null ? '–' : d.wait_min);
          set('wait_text', d.wait_text);
        }).catch(function () {});
      }
      setInterval(load, 15000);
    });
  }

  /* ---------- Tablet reminders ---------- */
  function enableNotifications() {
    if (!('Notification' in window)) { toast('Your browser does not support desktop alerts. On-page alerts will still show.'); return; }
    Notification.requestPermission().then(function (p) {
      toast(p === 'granted' ? 'Desktop alerts enabled.' : 'Alerts blocked. You will still see on-page alerts.', p === 'granted' ? 'toast-ok' : '');
    });
  }

  /* ---------- Tablet ALARM: rings until Taken / Snooze / Skip ---------- */
  var audioCtx = null, ringTimer = null, shownKeys = {}, soundHintShown = false;

  function getCtx() {
    if (!audioCtx) { try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { audioCtx = null; } }
    return audioCtx;
  }
  function unlockAudio() {
    var c = getCtx();
    if (c && c.state === 'suspended') { c.resume(); }
  }
  ['click', 'keydown', 'touchstart'].forEach(function (ev) { document.addEventListener(ev, unlockAudio, { passive: true }); });

  function tone(freq, start, dur) {
    var c = getCtx();
    if (!c || c.state !== 'running') { return; }
    var o = c.createOscillator(), g = c.createGain();
    o.type = 'square'; o.frequency.value = freq; g.gain.value = 0.15;
    o.connect(g); g.connect(c.destination);
    o.start(c.currentTime + start); o.stop(c.currentTime + start + dur);
  }
  function ringOnce() { tone(880, 0, .18); tone(660, .25, .18); tone(880, .5, .18); tone(660, .75, .18); }
  function beep() { unlockAudio(); ringOnce(); }

  function startRinging() {
    if (ringTimer) { return; }
    ringOnce();
    ringTimer = setInterval(ringOnce, 2000);
  }
  function stopRinging() { if (ringTimer) { clearInterval(ringTimer); ringTimer = null; } }

  function post(url, item, status) {
    var data = { csrf_token: csrf, medication_id: item.medication_id, slot: item.slot };
    if (status) { data.status = status; }
    return fetch(url, { method: 'POST', body: new URLSearchParams(data), headers: { 'X-Requested-With': 'fetch' } });
  }

  function refreshIfListPage() {
    if (location.pathname === '/reminders' || location.pathname === '/dashboard') { setTimeout(function () { location.reload(); }, 600); }
  }

  function alarmAct(item, kind, row) {
    var req = kind === 'Snooze' ? post('/reminders/snooze', item) : post('/reminders/log', item, kind);
    req.then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { toast(esc(res.d.message || 'Could not save. Please try again.')); return; }
        row.remove();
        if (kind === 'Snooze') { toast('Snoozed. I will ring again at ' + esc(res.d.until) + '.', 'toast-ok'); }
        else { toast(esc(item.name) + ' marked ' + kind.toLowerCase() + '.', 'toast-ok'); }
        var box = document.getElementById('alarm-overlay');
        if (box && !box.querySelector('.alarm-row')) { closeAlarm(); }
        refreshIfListPage();
      });
  }

  function closeAlarm() {
    stopRinging();
    var box = document.getElementById('alarm-overlay');
    if (box) { box.remove(); }
  }

  function showAlarm(items) {
    var sig = items.map(function (i) { return i.alert_key; }).join('|');
    var box = document.getElementById('alarm-overlay');
    if (box && box.dataset.sig === sig) { startRinging(); return; }
    if (box) { box.remove(); }
    box = document.createElement('div');
    box.id = 'alarm-overlay'; box.className = 'alarm-overlay'; box.dataset.sig = sig;
    box.setAttribute('role', 'alertdialog');
    box.innerHTML = '<div class="alarm-box"><h2>&#128138; Time for your tablet</h2>' +
      '<p class="muted small">Tap <b>I took it</b> when done. <b>Snooze</b> rings again in 5 minutes.' +
      ' (No sound? Click anywhere on this box once.)</p><div class="alarm-list"></div></div>';
    var list = box.querySelector('.alarm-list');
    items.forEach(function (item) {
      var row = document.createElement('div');
      row.className = 'alarm-row';
      row.innerHTML = '<b>' + esc(item.name) + '</b>' + (item.dosage ? ' <span class="muted">' + esc(item.dosage) + '</span>' : '') +
        '<div class="muted small">Scheduled ' + esc(item.time) + (item.notes ? ' · ' + esc(item.notes) : '') + '</div>' +
        '<div class="actions"><button class="btn btn-success" data-a="Taken">&#10004; I took it</button>' +
        '<button class="btn btn-outline" data-a="Snooze">&#9200; Snooze 5 min</button>' +
        '<button class="btn btn-ghost btn-xs" data-a="Skipped">Skip</button></div>';
      row.querySelectorAll('[data-a]').forEach(function (b) { b.onclick = function () { alarmAct(item, b.dataset.a, row); }; });
      list.appendChild(row);
    });
    document.body.appendChild(box);
    startRinging();
  }

  function checkReminders() {
    fetch('/api/reminders/due').then(function (r) { return r.ok ? r.json() : { due: [] }; }).then(function (data) {
      if (!data.due.length) { closeAlarm(); }
      else {
        data.due.forEach(function (item) {
          if (shownKeys[item.alert_key]) { return; }
          shownKeys[item.alert_key] = 1;
          if ('Notification' in window && Notification.permission === 'granted') {
            new Notification('MediKiosk tablet reminder', { body: 'Time to take ' + item.name + (item.dosage ? ' (' + item.dosage + ')' : '') + ' – ' + item.time });
          }
        });
        showAlarm(data.due);
      }
      var c = getCtx();
      if (data.has_meds && c && c.state !== 'running' && !soundHintShown) {
        soundHintShown = true;
        toast('Tip: click anywhere on the page once so the tablet alarm can play sound.');
      }
    }).catch(function () {});
  }

  function addTime() {
    var input = document.createElement('input');
    input.type = 'time'; input.name = 'times';
    document.getElementById('times').appendChild(input);
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-enable-notify]').forEach(function (b) { b.onclick = enableNotifications; });
    document.querySelectorAll('[data-enable-sound]').forEach(function (b) { b.onclick = function () { beep(); toast('If you heard the alarm, sound is on.', 'toast-ok'); }; });
    startLiveAppointments();
    if (loggedIn) { checkReminders(); setInterval(checkReminders, 15000); }
  });

  return { startQueueBoard: startQueueBoard, addTime: addTime, toast: toast };
})();
