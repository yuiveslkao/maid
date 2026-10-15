// maid: Open-LLM-VTuber の画面に VRM の 3D モデルを表示する。
//
// 画面の本体は改造しない。scripts/olv_patch.py がこのファイルを frontend/maid-vrm/ にコピーし、index.html から読み込ませる。
// - Live2D は透明にして残す（画面の本体は Live2D のモデル経由で声を再生するので、消すと声が出なくなる）
// - その上に VRM を重ねて描く。呼吸・まばたき・視線・声に合わせた口の動き
// - 設定は maid-defaults.js の window.MAID_VRM（maid.settings.json の vrm から作られる）
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";

const cfg = window.MAID_VRM;
if (cfg && cfg.url) start(cfg);

function start(cfg) {
  const fps = cfg.fps || 30; // GPU は頭脳と共用なので控えめに
  const style = document.createElement("style");
  style.textContent = "#canvas { opacity: 0 !important; }";
  document.head.appendChild(style);

  waitFor("#live2d-internal-wrapper").then((wrapper) => {
    const canvas = document.createElement("canvas");
    canvas.id = "maid-vrm-canvas";
    canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;";
    const status = document.createElement("div");
    status.style.cssText = "position:absolute;left:12px;bottom:12px;color:#fff;font-size:12px;text-shadow:0 0 4px #000;pointer-events:none;";
    wrapper.appendChild(canvas);
    wrapper.appendChild(status);
    // 画面が作り直されたら付け直す
    new MutationObserver(() => {
      if (!canvas.isConnected) {
        const w = document.querySelector("#live2d-internal-wrapper");
        if (w) { w.appendChild(canvas); w.appendChild(status); }
      }
    }).observe(document.body, { childList: true, subtree: true });
    run(canvas, status, cfg, fps).catch((e) => {
      console.error("[maid-vrm]", e);
      status.textContent = "3D モデルを読み込めませんでした: " + e.message;
    });
  });
}

function waitFor(selector) {
  return new Promise((resolve) => {
    const find = () => {
      const el = document.querySelector(selector);
      if (el) resolve(el); else setTimeout(find, 200);
    };
    find();
  });
}

async function run(canvas, status, cfg, fps) {
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 20);
  const light = new THREE.DirectionalLight(0xffffff, Math.PI);
  light.position.set(1, 1, 1).normalize();
  scene.add(light);

  status.textContent = "3D モデルを読み込んでいます...";
  const loader = new GLTFLoader();
  loader.register((parser) => new VRMLoaderPlugin(parser));
  const gltf = await loader.loadAsync(cfg.url, (e) => {
    if (e.total) status.textContent = `3D モデルを読み込んでいます... ${Math.round((e.loaded / e.total) * 100)}%`;
  });
  const vrm = gltf.userData.vrm;
  if (!vrm) throw new Error("VRM ではありません");
  VRMUtils.removeUnnecessaryVertices(gltf.scene);
  VRMUtils.combineSkeletons(gltf.scene);
  VRMUtils.rotateVRM0(vrm); // VRM 0.x は向きが逆なのでこちらを向かせる
  vrm.scene.traverse((o) => { o.frustumCulled = false; });
  scene.add(vrm.scene);
  status.textContent = "";

  const bone = (name) => vrm.humanoid.getNormalizedBoneNode(name);
  // T ポーズのままにしない（腕を下ろす）
  const pose = { leftUpperArm: [0, 0, -1.25], rightUpperArm: [0, 0, 1.25], leftLowerArm: [0, 0, -0.15], rightLowerArm: [0, 0, 0.15] };
  // VRM 0.x は回す向き（x と z）が VRM 1.0 と逆
  const flip = vrm.meta?.metaVersion === "0" ? -1 : 1;
  for (const [name, [x, y, z]] of Object.entries(pose)) bone(name)?.rotation.set(x * flip, y, z * flip);

  // カメラ: 頭の高さに合わせる（bust = 胸から上 / full = 全身）
  vrm.scene.updateMatrixWorld(true);
  const head = new THREE.Vector3();
  (bone("head") || vrm.scene).getWorldPosition(head);
  const tan = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  let target;
  if (cfg.view === "full") {
    target = new THREE.Vector3(0, head.y / 2 + 0.05, 0);
    camera.position.set(0, target.y, ((head.y + 0.25) / 2 / tan) * 1.1);
  } else {
    target = new THREE.Vector3(0, head.y - 0.15, 0);
    camera.position.set(0, head.y - 0.08, 0.45 / tan);
  }
  camera.lookAt(target);
  if (vrm.lookAt) vrm.lookAt.target = camera;

  const resize = () => {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  new ResizeObserver(resize).observe(canvas);
  resize();

  const lip = lipSync();
  const em = vrm.expressionManager;
  const spine = bone("spine"), chest = bone("chest") || spine, neck = bone("neck");
  const timer = new THREE.Timer();
  let blinkAt = 2, mouth = 0, acc = 0;

  renderer.setAnimationLoop(() => {
    timer.update();
    acc += timer.getDelta();
    if (acc < 1 / fps) return;
    const step = acc;
    acc = 0;
    const t = timer.getElapsed();

    // 呼吸とゆらぎ
    if (chest) chest.rotation.x = Math.sin(t * 1.6) * 0.015;
    if (spine) spine.rotation.z = Math.sin(t * 0.5) * 0.02;
    if (neck) { neck.rotation.y = Math.sin(t * 0.35) * 0.05; neck.rotation.z = Math.sin(t * 0.27) * 0.02; }

    if (em) {
      // まばたき（2〜6 秒ごと）
      const b = t - blinkAt;
      em.setValue("blink", b < 0 ? 0 : b < 0.08 ? b / 0.08 : b < 0.16 ? 1 - (b - 0.08) / 0.08 : 0);
      if (b >= 0.16) blinkAt = t + 2 + Math.random() * 4;
      // 口: 再生中の声の大きさ
      mouth += (lip.level() - mouth) * 0.6;
      em.setValue("aa", Math.min(1, mouth));
    }
    vrm.update(step);
    renderer.render(scene, camera);
  });
}

// 画面の本体が再生する声（data:audio の Audio 要素）を見て、今の声の大きさを返す。
// 再生そのものには手を出さない（同じデータを別に読み、音量の時間変化を先に計算しておく）
function lipSync() {
  const RATE = 60; // 1 秒あたりの区切り
  let current = null;
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function (...args) {
    if (typeof this.src === "string" && this.src.startsWith("data:audio")) {
      current = this;
      if (!this._maidEnvelope) envelope(this.src).then((env) => { this._maidEnvelope = env; }).catch(() => {});
    }
    return origPlay.apply(this, args);
  };

  async function envelope(src) {
    const data = await (await fetch(src)).arrayBuffer();
    const audio = await new OfflineAudioContext(1, 1, 44100).decodeAudioData(data);
    const ch = audio.getChannelData(0);
    const win = Math.max(1, Math.floor(audio.sampleRate / RATE));
    const out = new Float32Array(Math.ceil(ch.length / win));
    for (let i = 0; i < out.length; i++) {
      let sum = 0;
      const end = Math.min(ch.length, (i + 1) * win);
      for (let j = i * win; j < end; j++) sum += ch[j] * ch[j];
      out[i] = Math.min(1, Math.sqrt(sum / win) * 8);
    }
    return out;
  }

  return {
    level() {
      const a = current;
      if (!a || a.paused || a.ended || !a._maidEnvelope) return 0;
      return a._maidEnvelope[Math.floor(a.currentTime * RATE)] || 0;
    },
  };
}
