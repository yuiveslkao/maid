// maid: デスクトップのキャラのウィンドウで、キャラの声を鳴らす。
// Discord ゲート（/maid-observer）から声の写しを受け取り、届いた順に再生する。相手が割り込んだら止める。
// 口の動きは maid-vrm.js が、再生中の声から作る。チャットのウィンドウの声は消音してある（二重に聞こえないように）。
(function () {
  var queue = [];
  var current = null;

  function playNext() {
    if (current || !queue.length) return;
    var audio = new Audio("data:audio/wav;base64," + queue.shift());
    current = audio;
    var done = function () {
      if (current === audio) current = null;
      playNext();
    };
    audio.onended = done;
    audio.onerror = done;
    audio.play().catch(done);
  }

  function stop() {
    queue.length = 0;
    if (current) {
      current.pause();
      current = null;
    }
  }

  function connect() {
    var url = (window.MAID_GATE || "ws://127.0.0.1:12394") + "/maid-observer";
    var ws = new WebSocket(url);
    ws.onmessage = function (e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      if (msg.type === "audio" && msg.audio) {
        queue.push(msg.audio);
        playNext();
      } else if (msg.type === "maid-interrupt") {
        stop();
      }
    };
    ws.onclose = function () { setTimeout(connect, 2000); };  // maid の再起動などで切れたら、つなぎ直す
  }

  connect();
})();
