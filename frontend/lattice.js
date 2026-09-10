/* Clio — iris. Offline, dependency-free custom element.
   Concentric blade rings that dilate, drift, fragment and lock.
   State is read from the aperture's geometry and behaviour, not from a label.
   <clio-lattice state="standby|listening|thinking|speaking|muted|offline" level="0..1"></clio-lattice> */
(function () {
  if (customElements.get('clio-lattice')) return;

  var ACCENT = {
    standby:   [0xC8, 0xC3, 0xB9],
    listening: [0xF6, 0xF1, 0xE7],
    thinking:  [0x9F, 0xB4, 0xBC],
    speaking:  [0xF0, 0xA8, 0x3C],
    muted:     [0xB4, 0x6A, 0x52],
    offline:   [0x8A, 0x90, 0x98]
  };
  /* per state: aperture (0 shut … 1 wide), ink, blade break-up, spin, jitter */
  var SPEC = {
    standby:   { open: 0.46, ink: 0.34, gap: 0.30, spin:  0.10, chaos: 0.00 },
    listening: { open: 0.94, ink: 0.72, gap: 0.10, spin:  0.05, chaos: 0.00 },
    thinking:  { open: 0.62, ink: 0.52, gap: 0.46, spin:  0.70, chaos: 0.10 },
    speaking:  { open: 0.80, ink: 0.68, gap: 0.20, spin: -0.28, chaos: 0.00 },
    muted:     { open: 0.26, ink: 0.48, gap: 0.06, spin:  0.02, chaos: 0.00 },
    offline:   { open: 0.40, ink: 0.52, gap: 0.72, spin:  0.05, chaos: 1.00 }
  };
  var KEYS = Object.keys(ACCENT);

  var RINGS = [
    { r: 0.20, blades: 3, wob: 0.9, dir:  1, lw: 1.5 },
    { r: 0.31, blades: 5, wob: 1.3, dir: -1, lw: 1.2 },
    { r: 0.43, blades: 4, wob: 1.0, dir:  1, lw: 1.5 },
    { r: 0.55, blades: 7, wob: 1.6, dir: -1, lw: 1.0 },
    { r: 0.68, blades: 5, wob: 1.2, dir:  1, lw: 1.2 },
    { r: 0.82, blades: 9, wob: 1.9, dir: -1, lw: 0.9 },
    { r: 0.96, blades: 6, wob: 1.4, dir:  1, lw: 1.0 }
  ];
  var TAU = Math.PI * 2;

  function hash(i) { var x = Math.sin(i * 127.1) * 43758.5453; return x - Math.floor(x); }
  function lerp(a, b, t) { return a + (b - a) * t; }

  class ClioLattice extends HTMLElement {
    static get observedAttributes() { return ['state', 'level']; }

    connectedCallback() {
      if (this._built) return;
      this._built = true;
      this.style.display = 'block';
      this.style.position = this.style.position || 'relative';
      if (!this.style.width) this.style.width = '100%';
      if (!this.style.height) this.style.height = '100%';

      this.canvas = document.createElement('canvas');
      this.canvas.style.cssText = 'display:block;width:100%;height:100%';
      this.appendChild(this.canvas);
      this.ctx = this.canvas.getContext('2d');

      this.w = {}; KEYS.forEach(function (k) { this.w[k] = 0; }, this);
      this.w[this.state] = 1;
      this.lvl = 0;
      this.spin = 0;          // integrated rotation — speed changes never jump-cut
      this.breathe = 0;
      this.rings = [];        // emitted pulse rings
      this._flash = 0;        // divine-flash bloom, decays after a wake/pulse
      this.motes = [];        // embers rising in the void — ascension texture
      for (var mi = 0; mi < 26; mi++) this.motes.push(this._mote(true));
      this.t0 = performance.now(); this.last = this.t0;
      this._prevState = this.state;

      this._ro = new ResizeObserver(this.resize.bind(this));
      this._ro.observe(this);
      this.resize();

      this._reduce = matchMedia('(prefers-reduced-motion: reduce)');
      this._loop = this.frame.bind(this);
      requestAnimationFrame(this._loop);
    }

    disconnectedCallback() { if (this._ro) this._ro.disconnect(); this._built = false; }
    attributeChangedCallback() { }

    get state() {
      var s = this.getAttribute('state') || 'standby';
      if (s === 'idle') s = 'standby';
      return SPEC[s] ? s : 'standby';
    }

    pulse() { this.rings.push({ t: 0, gain: 1.25 }); this._flash = 1; }

    // one rising ember. seed=true scatters it anywhere; otherwise it enters from below.
    _mote(seed) {
      return {
        x: Math.random(),
        y: seed ? Math.random() : 1.06,
        r: 0.6 + Math.random() * 1.6,
        v: 0.012 + Math.random() * 0.03,   // rise speed, fraction of height per second
        ph: Math.random() * TAU,
        dph: 0.3 + Math.random() * 0.5,    // horizontal sway rate
        tw: 0.4 + Math.random() * 0.6      // twinkle rate
      };
    }

    resize() {
      var r = this.getBoundingClientRect();
      var d = Math.min(window.devicePixelRatio || 1, 2);
      this.W = Math.max(1, Math.round(r.width));
      this.H = Math.max(1, Math.round(r.height));
      this.canvas.width = Math.round(this.W * d);
      this.canvas.height = Math.round(this.H * d);
      this.ctx.setTransform(d, 0, 0, d, 0, 0);
      this._veil = null;
    }

    frame(now) {
      if (!this._built) return;
      var dt = Math.min(0.05, (now - this.last) / 1000); this.last = now;
      var ts = (now - this.t0) / 1000;
      var cur = this.state;

      if (cur !== this._prevState) {
        // every state change throws one ring — the iris always acknowledges
        this.rings.push({ t: 0, gain: cur === 'listening' ? 1.1 : 0.7 });
        this._prevState = cur;
      }

      var k = 1 - Math.exp(-dt * 3.4);
      for (var i = 0; i < KEYS.length; i++) {
        var key = KEYS[i];
        this.w[key] += ((key === cur ? 1 : 0) - this.w[key]) * k;
      }

      var target = parseFloat(this.getAttribute('level'));
      if (isNaN(target)) {
        target = cur === 'listening'
          ? 0.34 + 0.50 * Math.abs(Math.sin(ts * 1.4) * Math.sin(ts * 0.53 + 1.1))
          : cur === 'speaking'
            ? 0.44 + 0.48 * Math.abs(Math.sin(ts * 2.2 + Math.sin(ts * 0.9) * 1.4))
            : 0.26;
      }
      this.lvl += (target - this.lvl) * (1 - Math.exp(-dt * 8));

      var spinRate = 0, tot = 0;
      for (var m = 0; m < KEYS.length; m++) {
        var q = this.w[KEYS[m]]; if (q < 0.002) continue;
        spinRate += SPEC[KEYS[m]].spin * q; tot += q;
      }
      this.spin += dt * (spinRate / (tot || 1)) * (1 + this.lvl * 0.4);
      this.breathe += dt;

      // speaking emits rings on level peaks; listening emits slowly
      this._emit = (this._emit || 0) - dt;
      if (this._emit <= 0) {
        if (this.w.speaking > 0.5) { this.rings.push({ t: 0, gain: 0.35 + this.lvl * 0.7 }); this._emit = 0.42; }
        else if (this.w.listening > 0.5) { this.rings.push({ t: 0, gain: 0.22 + this.lvl * 0.4 }); this._emit = 1.05; }
        else this._emit = 0.5;
      }
      for (var r2 = this.rings.length - 1; r2 >= 0; r2--) {
        this.rings[r2].t += dt;
        if (this.rings[r2].t > 2.2) this.rings.splice(r2, 1);
      }

      this._flash = Math.max(0, this._flash - dt * 2.2);

      // embers drift up and sway; recycle from below when they clear the top
      var rise = 1 + this.lvl * 0.7 + this.w.speaking * 0.5;
      for (var mm = 0; mm < this.motes.length; mm++) {
        var mo = this.motes[mm];
        mo.y -= mo.v * dt * rise;
        mo.x += Math.sin(ts * mo.dph + mo.ph) * 0.0003;
        if (mo.y < -0.05) this.motes[mm] = this._mote(false);
      }

      this.draw(this._reduce.matches ? 4.0 : ts, this._reduce.matches);
      requestAnimationFrame(this._loop);
    }

    draw(ts, still) {
      var ctx = this.ctx, W = this.W, H = this.H, w = this.w, lvl = this.lvl;
      var spin = still ? 0.4 : this.spin;
      var br = still ? 1.0 : this.breathe;

      ctx.fillStyle = 'rgb(5,6,10)';
      ctx.fillRect(0, 0, W, H);

      var cr = 0, cg = 0, cb = 0, open = 0, ink = 0, gapF = 0, chaos = 0, tot = 0;
      for (var m = 0; m < KEYS.length; m++) {
        var kk = KEYS[m], q = w[kk]; if (q < 0.002) continue;
        var sp = SPEC[kk];
        cr += ACCENT[kk][0] * q; cg += ACCENT[kk][1] * q; cb += ACCENT[kk][2] * q;
        open += sp.open * q; ink += sp.ink * q; gapF += sp.gap * q; chaos += sp.chaos * q;
        tot += q;
      }
      if (tot < 0.001) tot = 1;
      cr = Math.round(cr / tot); cg = Math.round(cg / tot); cb = Math.round(cb / tot);
      open /= tot; ink /= tot; gapF /= tot; chaos /= tot;
      var rgb = cr + ',' + cg + ',' + cb;

      var cx = W / 2, cy = H * 0.43;
      var R = Math.min(W, H) * 0.355;
      // breathing dilation + live level ride
      var dil = open * (1 + 0.045 * Math.sin(br * 0.9)) * (0.86 + lvl * 0.22);
      var base = R * dil;

      /* ── core glow (blooms on a wake flash) ─────── */
      var flash = this._flash * this._flash;   // ease-in decay
      var glowR = base * (0.9 + flash * 0.55);
      var glow = ctx.createRadialGradient(cx, cy, 1, cx, cy, Math.max(glowR, 6));
      glow.addColorStop(0, 'rgba(' + rgb + ',' + (0.20 * ink + lvl * 0.12 + flash * 0.55).toFixed(3) + ')');
      glow.addColorStop(0.45, 'rgba(' + rgb + ',' + (0.05 * ink + flash * 0.18).toFixed(3) + ')');
      glow.addColorStop(1, 'rgba(' + rgb + ',0)');
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, W, H);

      /* ── rising embers: dust ascending through the void ── */
      for (var mm = 0; mm < this.motes.length; mm++) {
        var mo = this.motes[mm];
        var tw = 0.45 + 0.55 * Math.sin(ts * mo.tw * 2 + mo.ph);
        var ma = (0.09 + 0.20 * ink) * tw * (0.5 + lvl * 0.6);
        if (ma < 0.004) continue;
        ctx.beginPath();
        ctx.arc(mo.x * W, mo.y * H, mo.r, 0, TAU);
        ctx.fillStyle = 'rgba(' + rgb + ',' + ma.toFixed(3) + ')';
        ctx.fill();
      }

      ctx.lineCap = 'round';

      /* ── emitted rings ─────────────────────────── */
      for (var e = 0; e < this.rings.length; e++) {
        var ring = this.rings[e];
        var p = ring.t / 2.2;
        var er = base * (0.28 + p * 1.55);
        var ea = ink * ring.gain * Math.pow(1 - p, 2.2) * 0.9;
        if (ea < 0.004) continue;
        ctx.beginPath();
        ctx.arc(cx, cy, er, 0, TAU);
        ctx.lineWidth = 0.6 + (1 - p) * 1.0;
        ctx.strokeStyle = 'rgba(' + rgb + ',' + ea.toFixed(3) + ')';
        ctx.stroke();
      }

      /* ── blade rings ───────────────────────────── */
      for (var i = 0; i < RINGS.length; i++) {
        var Rg = RINGS[i];
        var depth = i / (RINGS.length - 1);
        var rr = base * Rg.r * (1 + 0.03 * Math.sin(br * 1.3 + i * 0.8));
        if (rr < 1) continue;

        var rot = spin * Rg.dir * (0.6 + depth * 1.5) + i * 0.7;
        // blades separate as gap grows; they lock shoulder-to-shoulder when listening
        var seg = TAU / Rg.blades;
        var gapArc = seg * lerp(0.04, 0.62, gapF) * (1 + 0.35 * Math.sin(br * Rg.wob + i));
        var arc = seg - gapArc;

        var a = ink * (1 - depth * 0.42) * (0.75 + lvl * 0.4);
        ctx.lineWidth = Rg.lw * (0.7 + (1 - depth) * 0.6);

        for (var b = 0; b < Rg.blades; b++) {
          var wob = chaos * (hash(i * 31 + b) - 0.5) * 1.5;
          var drift = chaos * (hash(i * 71 + b + 5) - 0.5) * rr * 0.28;
          var a0 = rot + b * seg + gapArc * 0.5 + wob;
          var a1 = a0 + arc;
          var rBlade = rr + drift;
          if (rBlade < 1) continue;
          ctx.beginPath();
          ctx.arc(cx, cy, rBlade, a0, a1);
          ctx.strokeStyle = 'rgba(' + rgb + ',' + (a * (1 - chaos * 0.35)).toFixed(3) + ')';
          ctx.stroke();

          // blade tips: tiny radial ticks that make the aperture feel mechanical
          if (i % 2 === 0 && chaos < 0.4) {
            var tipA = a * 0.7;
            ctx.beginPath();
            ctx.moveTo(cx + Math.cos(a0) * (rBlade - 3), cy + Math.sin(a0) * (rBlade - 3));
            ctx.lineTo(cx + Math.cos(a0) * (rBlade + 3), cy + Math.sin(a0) * (rBlade + 3));
            ctx.lineWidth = 0.7;
            ctx.strokeStyle = 'rgba(' + rgb + ',' + tipA.toFixed(3) + ')';
            ctx.stroke();
          }
        }
      }

      /* ── thinking: a sweep hand that rakes the aperture ── */
      if (w.thinking > 0.02) {
        var sa = spin * 2.4;
        var grad = ctx.createLinearGradient(
          cx, cy, cx + Math.cos(sa) * base, cy + Math.sin(sa) * base
        );
        grad.addColorStop(0, 'rgba(' + rgb + ',0)');
        grad.addColorStop(1, 'rgba(' + rgb + ',' + (0.42 * w.thinking).toFixed(3) + ')');
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(cx + Math.cos(sa) * base, cy + Math.sin(sa) * base);
        ctx.lineWidth = 1;
        ctx.strokeStyle = grad;
        ctx.stroke();
      }

      /* ── pupil ─────────────────────────────────── */
      var pupil = base * lerp(0.10, 0.055, open) * (1 + lvl * 0.5);
      ctx.beginPath();
      ctx.arc(cx, cy, Math.max(1.2, pupil), 0, TAU);
      ctx.fillStyle = 'rgba(' + rgb + ',' + (0.55 + lvl * 0.4).toFixed(3) + ')';
      ctx.fill();

      /* ── outer bezel: fixed reference the iris moves against ── */
      var bez = R * 1.06;
      ctx.beginPath();
      ctx.arc(cx, cy, bez, 0, TAU);
      ctx.lineWidth = 1;
      ctx.strokeStyle = 'rgba(' + rgb + ',0.07)';
      ctx.stroke();
      for (var t = 0; t < 60; t++) {
        var ta = (t / 60) * TAU - Math.PI / 2;
        var major = t % 5 === 0;
        var len = major ? 5 : 2.5;
        var alpha = major ? 0.22 : 0.10;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(ta) * bez, cy + Math.sin(ta) * bez);
        ctx.lineTo(cx + Math.cos(ta) * (bez + len), cy + Math.sin(ta) * (bez + len));
        ctx.lineWidth = major ? 1 : 0.7;
        ctx.strokeStyle = 'rgba(' + rgb + ',' + alpha.toFixed(3) + ')';
        ctx.stroke();
      }

      /* ── level arc riding the bezel ─────────────── */
      if (lvl > 0.02 && (w.listening + w.speaking) > 0.05) {
        var span = TAU * 0.28 * lvl * (w.listening + w.speaking);
        ctx.beginPath();
        ctx.arc(cx, cy, bez + 8, -Math.PI / 2 - span / 2, -Math.PI / 2 + span / 2);
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = 'rgba(' + rgb + ',' + (0.5 * ink * 2).toFixed(3) + ')';
        ctx.stroke();
      }

      if (!this._veil || this._veilW !== W || this._veilH !== H) {
        var g = ctx.createLinearGradient(0, 0, 0, H);
        g.addColorStop(0, 'rgba(7,8,10,0.80)');
        g.addColorStop(0.14, 'rgba(7,8,10,0.06)');
        g.addColorStop(0.68, 'rgba(7,8,10,0)');
        g.addColorStop(0.88, 'rgba(7,8,10,0.80)');
        g.addColorStop(1, 'rgba(7,8,10,0.97)');
        this._veil = g; this._veilW = W; this._veilH = H;
      }
      ctx.fillStyle = this._veil;
      ctx.fillRect(0, 0, W, H);
    }
  }

  customElements.define('clio-lattice', ClioLattice);
})();
