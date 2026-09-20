(function () {
  var STATES = {
    standby:   { word:'Standby',   color:'#EDE8DE', note:'Wake word armed',        meter:'18%' },
    listening: { word:'Listening', color:'#EDE8DE', note:'Recording · local only', meter:'100%' },
    thinking:  { word:'Thinking',  color:'#9FB4BC', note:'Working it out',         meter:'62%' },
    speaking:  { word:'Speaking',  color:'#F0A83C', note:'Answering aloud',        meter:'84%' },
    muted:     { word:'Muted',     color:'#A85A44', note:'Microphone off',         meter:'0%' },
    offline:   { word:'No core',   color:'#6B7078', note:'Reconnecting to 8765',   meter:'0%' }
  };

  var lattice = document.querySelector('clio-lattice');
  var elState = document.getElementById('state');
  var elNote  = document.getElementById('note');
  var elMeter = document.getElementById('meter');
  var elDot   = document.getElementById('dot');
  var elLink  = document.getElementById('link');
  var elMic   = document.getElementById('mic');
  var elYou   = document.querySelector('#you .txt');
  var elClio  = document.querySelector('#clio .txt');

  var connected = false, current = 'offline';

  function render(name) {
    var s = STATES[connected ? name : 'offline'] || STATES.standby;
    elState.textContent = s.word;
    elState.style.color = s.color;
    elState.style.textShadow = connected
      ? '0 0 26px ' + s.color + '55, 0 0 9px ' + s.color + '30'
      : 'none';
    elNote.textContent = s.note;
    elMeter.style.width = s.meter;
    elMeter.style.background = s.color;
    lattice.setAttribute('state', connected ? name : 'offline');
    elDot.style.background = !connected ? '#3A3F46' : (name === 'muted' ? '#A85A44' : '#F0A83C');
    elLink.textContent = connected ? '127.0.0.1:8765' : 'not connected';
    elMic.textContent = name === 'muted' ? 'MIC OFF' : 'MIC LIVE';
    elMic.style.color = name === 'muted' ? '#A85A44' : '#464B52';
  }

  var bus = new ClioBus('ws://127.0.0.1:8765');
  bus.on('open', function () { connected = true; render(current); });
  bus.on('close', function () { connected = false; render('offline'); });
  bus.on('clio.state', function (p) {
    current = p.state === 'idle' ? 'standby' : (p.state || 'standby');
    render(current);
  });
  bus.on('clio.wake', function () { if (lattice.pulse) lattice.pulse(); });
  bus.on('clio.transcript', function (p) {
    if (!p || !p.text) return;
    (p.role === 'assistant' ? elClio : elYou).textContent = p.text;
  });

  // tray "Mute" item → toggle the mic through the core
  if (window.__TAURI__ && window.__TAURI__.event) {
    window.__TAURI__.event.listen('tray-mute', function () {
      bus.send({ cmd: 'mute', on: current !== 'muted' });
    });
  }

  render('offline');
})();
