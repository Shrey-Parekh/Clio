/* Moving, resizing and remembering the window.
 *
 * The windows have no native frame - decorations are off, which is what makes
 * the HUD look like an instrument rather than a dialog. The cost is that
 * everything a frame gives you for free has to exist here instead: dragging,
 * resizing from the edges, minimise, maximise, fullscreen, and coming back the
 * size he left it.
 *
 * The header markup carries `data-tauri-drag-region`, which Tauri handles
 * natively. What it replaced was `-webkit-app-region: drag`, which is
 * Electron's spelling and does nothing here - so the windows could not be
 * moved at all.
 *
 * Opened in an ordinary browser (no Tauri), everything below is skipped and
 * the page still works.
 */
(function () {
  /* --- size classes, so the CSS reacts to the window, not the screen ---
   * Measured here rather than in a media query because the window is what
   * changes: he drags it smaller, maximises it, or throws it on a second
   * monitor. Wired up before anything Tauri-specific, so the page still
   * adapts when it is opened in an ordinary browser.
   */
  function measure() {
    var root = document.documentElement;
    var w = window.innerWidth, h = window.innerHeight;
    root.setAttribute('data-width',
      w < 340 ? 'tiny' : w < 480 ? 'narrow' : w < 760 ? 'normal' : 'wide');
    root.setAttribute('data-height', h < 380 ? 'short' : h < 560 ? 'normal' : 'tall');
  }
  window.addEventListener('resize', measure);
  measure();

  var tauri = window.__TAURI__ && window.__TAURI__.window;
  if (!tauri) { document.documentElement.setAttribute('data-plain', ''); return; }

  var win = tauri.getCurrentWindow();
  var store = 'clio.window.' + win.label;
  var EDGE = 6, CORNER = 14;
  var pinned = win.label === 'main';

  /* --- resize handles -------------------------------------------------
   * Without decorations there are no borders to grab, so eight thin strips
   * sit over the edges and hand the drag straight to the window manager.
   * Windows does the resizing; this only says which way.
   */
  var DIRECTIONS = [
    ['North',     { top:0, left:CORNER, right:CORNER, height:EDGE, cursor:'ns-resize' }],
    ['South',     { bottom:0, left:CORNER, right:CORNER, height:EDGE, cursor:'ns-resize' }],
    ['West',      { left:0, top:CORNER, bottom:CORNER, width:EDGE, cursor:'ew-resize' }],
    ['East',      { right:0, top:CORNER, bottom:CORNER, width:EDGE, cursor:'ew-resize' }],
    ['NorthWest', { top:0, left:0, width:CORNER, height:CORNER, cursor:'nwse-resize' }],
    ['NorthEast', { top:0, right:0, width:CORNER, height:CORNER, cursor:'nesw-resize' }],
    ['SouthWest', { bottom:0, left:0, width:CORNER, height:CORNER, cursor:'nesw-resize' }],
    ['SouthEast', { bottom:0, right:0, width:CORNER, height:CORNER, cursor:'nwse-resize' }]
  ];

  function addHandles() {
    DIRECTIONS.forEach(function (pair) {
      var grip = document.createElement('div');
      grip.className = 'win-grip';
      grip.setAttribute('data-resize', pair[0]);
      Object.keys(pair[1]).forEach(function (key) {
        var value = pair[1][key];
        grip.style[key] = typeof value === 'number' ? value + 'px' : value;
      });
      grip.style.position = 'fixed';
      grip.style.zIndex = '999';
      document.body.appendChild(grip);
    });
  }

  document.addEventListener('mousedown', function (event) {
    if (event.button !== 0) return;
    var grip = event.target.closest && event.target.closest('[data-resize]');
    if (!grip) return;
    event.preventDefault();
    win.startResizeDragging(grip.getAttribute('data-resize'));
  });

  /* --- the buttons a title bar would have ---------------------------- */

  var actions = {
    minimise: function () { return win.minimize(); },
    maximise: function () { return win.toggleMaximize().then(paint); },
    fullscreen: function () {
      return win.isFullscreen().then(function (on) {
        return win.setFullscreen(!on).then(paint);
      });
    },
    // Closing the HUD hides it rather than quitting: the tray icon is where
    // Clio actually lives, and quitting by accident stops her listening.
    close: function () { return win.hide(); },
    pin: function () {
      pinned = !pinned;
      return win.setAlwaysOnTop(pinned).then(paint);
    }
  };

  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-win]');
    if (!button) return;
    var run = actions[button.getAttribute('data-win')];
    if (run) { event.preventDefault(); run(); }
  });

  // Double-clicking the drag strip maximises, the way a title bar does.
  document.addEventListener('dblclick', function (event) {
    if (event.target.closest && event.target.closest('[data-tauri-drag-region]')) {
      actions.maximise();
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'F11') {
      event.preventDefault();
      actions.fullscreen();
    } else if (event.key === 'Escape') {
      win.isFullscreen().then(function (on) {
        if (on) { win.setFullscreen(false).then(paint); }
      });
    } else if (event.ctrlKey && (event.key === 'm' || event.key === 'M')) {
      event.preventDefault();
      actions.minimise();
    }
  });

  function paint() {
    return Promise.all([win.isMaximized(), win.isFullscreen()]).then(function (both) {
      var root = document.documentElement;
      root.toggleAttribute('data-maximised', both[0] || both[1]);
      root.toggleAttribute('data-fullscreen', both[1]);
      var pin = document.querySelector('[data-win="pin"]');
      if (pin) {
        pin.setAttribute('aria-pressed', String(pinned));
        pin.title = pinned ? 'Always on top (on)' : 'Always on top (off)';
      }
    });
  }

  /* --- come back the size he left it --------------------------------- */

  var saving = null;
  function remember() {
    clearTimeout(saving);
    saving = setTimeout(function () {
      Promise.all([win.outerPosition(), win.outerSize(), win.isMaximized()])
        .then(function (state) {
          if (state[2]) return;      // a maximised window has no size worth keeping
          try {
            localStorage.setItem(store, JSON.stringify({
              x: state[0].x, y: state[0].y, w: state[1].width, h: state[1].height
            }));
          } catch (err) { /* private mode, or storage full: not worth a word */ }
        })
        .catch(function () {});
    }, 400);
  }

  function restore() {
    var saved;
    try { saved = JSON.parse(localStorage.getItem(store) || 'null'); } catch (err) { saved = null; }
    if (!saved || !saved.w || !saved.h) return Promise.resolve();
    // A monitor he has since unplugged would put the window somewhere he
    // cannot reach, so anything off the current screen is ignored.
    var onScreen = saved.x > -200 && saved.y > -50
      && saved.x < (window.screen.width || 4000)
      && saved.y < (window.screen.height || 3000);
    var move = onScreen
      ? win.setPosition(new tauri.PhysicalPosition(saved.x, saved.y))
      : Promise.resolve();
    return win.setSize(new tauri.PhysicalSize(saved.w, saved.h))
      .then(function () { return move; })
      .catch(function () {});
  }

  window.addEventListener('resize', remember);
  if (win.onMoved) { win.onMoved(remember); }

  addHandles();
  restore().then(paint).then(measure);
})();
