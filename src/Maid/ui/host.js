// maid: 画面と maid.exe の橋渡し（maid.exe がどの画面にも最初に読み込ませる）。
//   画面 → maid.exe: maidApi.<名前>(引数...) は Promise を返す（hear だけは返事を待たない）
//   maid.exe → 画面: window.maid.<名前>(...) を呼ぶ
(function () {
  var pending = {}, seq = 0, view = window.chrome && window.chrome.webview;
  if (!view) return;
  view.addEventListener("message", function (e) {
    var m = e.data, p = m && pending[m.id];
    if (!p) return;
    delete pending[m.id];
    if (m.error !== undefined) p.reject(new Error(m.error)); else p.resolve(m.result);
  });
  function call(method, args) {
    if (method === "hear") { view.postMessage({ method: method, args: args }); return Promise.resolve(); }
    var id = ++seq;
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      view.postMessage({ id: id, method: method, args: args });
    });
  }
  window.maidApi = new Proxy({}, {
    get: function (_, name) { return function () { return call(String(name), Array.prototype.slice.call(arguments)); }; }
  });
  document.addEventListener("DOMContentLoaded", function () { window.dispatchEvent(new Event("maidready")); });
})();
