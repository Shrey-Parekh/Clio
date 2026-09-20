/* Clio — WebSocket bus. Offline-safe, auto-reconnecting, no dependencies. */
(function (root) {
  var URL_ = 'ws://127.0.0.1:8765';

  function Bus(url) {
    this.url = url || URL_;
    this.ws = null;
    this.connected = false;
    this.seq = 100;
    this.pending = {};
    this.handlers = {};
    this.retry = 0;
    this.connect();
  }

  Bus.prototype.on = function (name, fn) {
    (this.handlers[name] || (this.handlers[name] = [])).push(fn);
    return this;
  };

  Bus.prototype.emit = function (name, arg) {
    var hs = this.handlers[name];
    if (hs) for (var i = 0; i < hs.length; i++) { try { hs[i](arg); } catch (e) { console.error(e); } }
  };

  Bus.prototype.connect = function () {
    var self = this;
    try { this.ws = new WebSocket(this.url); } catch (e) { return this.schedule(); }
    this.ws.onopen = function () {
      self.retry = 0; self.connected = true; self.emit('open');
    };
    this.ws.onclose = function () {
      self.connected = false; self.emit('close'); self.schedule();
    };
    this.ws.onerror = function () { try { self.ws.close(); } catch (e) { } };
    this.ws.onmessage = function (ev) {
      var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.req != null && self.pending[msg.req]) {
        self.pending[msg.req](msg.payload || {});
        delete self.pending[msg.req];
        return;
      }
      if (msg.name) self.emit(msg.name, msg.payload || {});
    };
  };

  Bus.prototype.schedule = function () {
    var delay = Math.min(8000, 600 * Math.pow(1.6, this.retry++));
    clearTimeout(this._t);
    this._t = setTimeout(this.connect.bind(this), delay);
  };

  Bus.prototype.send = function (obj) {
    if (!this.connected) return false;
    try { this.ws.send(JSON.stringify(obj)); return true; } catch (e) { return false; }
  };

  /* send with a reply callback */
  Bus.prototype.request = function (obj, cb) {
    var id = this.seq++;
    obj.id = id;
    if (cb) this.pending[id] = cb;
    if (!this.send(obj)) delete this.pending[id];
    return id;
  };

  root.ClioBus = Bus;
})(window);
