(function () {
  var $ = function (s) { return document.querySelector(s); };
  var connected = false, muted = false;

  /* tabs */
  var tabs = [].slice.call(document.querySelectorAll('nav button'));
  tabs.forEach(function (b) {
    b.addEventListener('click', function () {
      tabs.forEach(function (o) { o.setAttribute('aria-selected', String(o === b)); });
      document.querySelectorAll('.pane').forEach(function (p) {
        if (p.dataset.pane === b.dataset.tab) p.setAttribute('data-on', ''); else p.removeAttribute('data-on');
      });
    });
  });

  var bus = new ClioBus('ws://127.0.0.1:8765');

  function setLink(on) {
    connected = on;
    $('#dot').style.background = on ? '#F0A83C' : '#3A3F46';
    $('#linkTxt').textContent = on ? '127.0.0.1:8765' : 'not connected';
    [ '#sendBtn', '#addBtn', '#muteBtn', '#speed' ].forEach(function (s) { $(s).disabled = !on; });
  }

  /* chat */
  var thread = $('#thread'), threadScroll = thread.parentNode, empty = true;
  function addTurn(role, text) {
    if (empty) { thread.innerHTML = ''; empty = false; }
    var d = document.createElement('div');
    d.className = 'turn ' + (role === 'assistant' ? 'assistant' : 'user');
    var tag = document.createElement('div'); tag.className = 'tag';
    tag.textContent = role === 'assistant' ? 'CLIO' : 'YOU';
    var tx = document.createElement('div'); tx.className = 'txt'; tx.textContent = text;
    d.appendChild(tag); d.appendChild(tx); thread.appendChild(d);
    threadScroll.scrollTop = threadScroll.scrollHeight;
  }
  function say() {
    var v = $('#say').value.trim(); if (!v || !connected) return;
    bus.send({ cmd: 'say', text: v });
    $('#say').value = '';
  }
  $('#sendBtn').addEventListener('click', say);
  $('#say').addEventListener('keydown', function (e) { if (e.key === 'Enter') say(); });

  /* settings */
  $('#speed').addEventListener('input', function () {
    $('#speedVal').textContent = parseFloat(this.value).toFixed(2) + '×';
  });
  $('#speed').addEventListener('change', function () {
    bus.send({ cmd: 'tts_speed', value: parseFloat(this.value) });
  });
  function paintMute() {
    var b = $('#muteBtn');
    b.textContent = muted ? 'Muted' : 'Live';
    b.style.color = muted ? '#A85A44' : '';
    b.style.borderColor = muted ? '#3A2019' : '';
  }
  $('#muteBtn').addEventListener('click', function () {
    muted = !muted; paintMute(); bus.send({ cmd: 'mute', on: muted });
  });

  function paintSettings(p) {
    $('#persona').textContent = p.persona || '—';
    $('#voice').textContent = p.voice || '—';
    var w = $('#wake'); w.innerHTML = '';
    (p.wake_phrases || []).forEach(function (ph) {
      var c = document.createElement('div'); c.className = 'chip'; c.textContent = ph; w.appendChild(c);
    });
    if (typeof p.tts_speed === 'number') {
      $('#speed').value = p.tts_speed;
      $('#speedVal').textContent = p.tts_speed.toFixed(2) + '×';
    }
    muted = !!p.muted; paintMute();
    var caps = $('#caps'); caps.innerHTML = '';
    (p.capabilities || []).forEach(function (c) {
      var tier = (p.permissions && p.permissions[c.name]) || c.permission || 'free';
      var row = document.createElement('div'); row.className = 'cap';
      var nm = document.createElement('div'); nm.className = 'nm'; nm.textContent = c.name;
      var net = document.createElement('div');
      net.className = 'net' + (c.offline ? '' : ' on');
      net.textContent = c.offline ? 'local' : 'network';
      var t = document.createElement('div'); t.className = 'tier ' + tier; t.textContent = tier;
      row.appendChild(nm); row.appendChild(net); row.appendChild(t); caps.appendChild(row);
    });
  }

  /* memory */
  function paintFacts(list) {
    var box = $('#facts'); box.innerHTML = '';
    $('#memHead').textContent = list.length
      ? list.length + ' things Clio remembers'
      : 'Nothing remembered yet';
    list.forEach(function (f, i) {
      var d = document.createElement('div'); d.className = 'fact';
      var n = document.createElement('div'); n.className = 'n';
      n.textContent = String(i + 1).padStart(2, '0');
      var t = document.createElement('div'); t.className = 't'; t.textContent = f;
      d.appendChild(n); d.appendChild(t); box.appendChild(d);
    });
  }
  function addFact() {
    var v = $('#fact').value.trim(); if (!v || !connected) return;
    bus.request({ cmd: 'add_fact', text: v }, function (r) {
      if (r && r.added) { $('#fact').value = ''; bus.request({ cmd: 'get_memory' }, function (m) { paintFacts(m.facts || []); }); }
    });
  }
  $('#addBtn').addEventListener('click', addFact);
  $('#fact').addEventListener('keydown', function (e) { if (e.key === 'Enter') addFact(); });

  /* wiring */
  bus.on('open', function () {
    setLink(true);
    bus.request({ cmd: 'get_settings' }, paintSettings);
    bus.request({ cmd: 'get_memory' }, function (m) { paintFacts(m.facts || []); });
  });
  bus.on('close', function () { setLink(false); });
  bus.on('clio.transcript', function (p) { if (p && p.text) addTurn(p.role, p.text); });

  setLink(false);
  paintFacts([]);
})();
