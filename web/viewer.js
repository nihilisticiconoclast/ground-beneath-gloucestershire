// The Ground Beneath Gloucestershire — voxel viewer.
//
// Loads data/sample_voxels.json (or whatever data/model.json the build writes),
// renders every below-ground voxel as an instance of one box, and gives the
// visitor two dials: peel the ground away down to an elevation, and hide voxels
// the model is unsure about. Everything is precomputed offline; the browser
// only draws.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

// Lithology colours: broadly the conventions of British geological maps, tuned
// so neighbours in the Stroud stack (limestone / mudstone / clay) stay distinct.
const LITH_COLOURS = {
  TOPSOIL: "#5E4529",
  MADE_GROUND: "#8C8C84",
  PEAT: "#3A2A1F",
  CLAY: "#A8925A",
  SILT: "#CDBE8E",
  SAND: "#E4C973",
  GRAVEL: "#D89D5A",
  MARL: "#B96C55",
  MUDSTONE: "#667B8C",
  SILTSTONE: "#8C9CA8",
  SANDSTONE: "#C68B52",
  LIMESTONE: "#E7DAA9",
  IRONSTONE: "#7C4739",
  COAL: "#2A2A2A",
  CHALK: "#F0EEE4",
  NO_RECOVERY: "#D7D7D2",
  UNKNOWN: "#C9C9C4",
};
const AIR = 255;
const HIDDEN = new THREE.Matrix4().makeScale(0, 0, 0);

const $ = (id) => document.getElementById(id);

async function loadModel() {
  const params = new URLSearchParams(location.search);
  const url = params.get("model") || "data/sample_voxels.json";
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`Could not load ${url}: HTTP ${resp.status}`);
  const model = await resp.json();
  const n = model.nx * model.ny * model.nz;
  if (model.class_idx.length !== n || model.entropy.length !== n) {
    throw new Error(`Model ${url} is malformed: expected ${n} voxels`);
  }
  return model;
}

function buildScene(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(canvas.clientWidth, canvas.clientHeight, false);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, canvas.clientWidth / canvas.clientHeight, 1, 100000);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxPolarAngle = Math.PI * 0.49;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x7a7466, 1.05));
  const sun = new THREE.DirectionalLight(0xffffff, 1.3);
  sun.position.set(-0.6, 1, 0.4);
  scene.add(sun);
  return { renderer, scene, camera, controls };
}

function main() {
  const canvas = $("scene");
  const { renderer, scene, camera, controls } = buildScene(canvas);

  loadModel()
    .then((model) => setup(model, { renderer, scene, camera, controls, canvas }))
    .catch((err) => {
      $("model-name").textContent = err.message;
      $("model-name").style.color = "#8A2E22";
    });
}

function setup(model, ctx) {
  const { renderer, scene, camera, controls, canvas } = ctx;
  const { nx, ny, nz, cell_xy: cxy, cell_z: cz, z0 } = model;
  const n = nx * ny * nz;

  $("model-name").textContent = model.name || "model";
  const flag = $("synthetic-flag");
  if (model.synthetic) {
    flag.textContent = "Synthetic preview. Nothing here is measured yet.";
    flag.hidden = false;
  } else if (model.gates_passed === false) {
    flag.textContent = "Baked with failed validation gates. Treat as a draft.";
    flag.hidden = false;
  } else if (model.frame === "depth") {
    flag.textContent = "Depth frame: no ground levels, so z is depth below ground, not elevation.";
    flag.hidden = false;
  } else {
    flag.hidden = true;
  }

  // World units are metres; the grid is centred on the origin, z up, vertical
  // exaggeration so a 75 m column reads against a 5 km footprint.
  const VEXAG = 6;
  const width = nx * cxy, depth = ny * cxy;
  const zTop = z0 + nz * cz;

  // One instance per below-ground voxel.
  const below = [];
  for (let i = 0; i < n; i++) if (model.class_idx[i] !== AIR) below.push(i);
  const geometry = new THREE.BoxGeometry(cxy * 0.98, cz * VEXAG * 0.96, cxy * 0.98);
  const material = new THREE.MeshLambertMaterial({ vertexColors: false });
  const mesh = new THREE.InstancedMesh(geometry, material, below.length);
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  const colour = new THREE.Color();
  const matrix = new THREE.Matrix4();
  const positions = new Float32Array(below.length * 3);
  const counts = {};
  below.forEach((idx, k) => {
    const i = idx % nx, j = Math.floor(idx / nx) % ny, kz = Math.floor(idx / (nx * ny));
    const x = (i + 0.5) * cxy - width / 2;
    const y = (kz + 0.5) * cz * VEXAG; // elevation above z0, exaggerated
    const zw = -((j + 0.5) * cxy - depth / 2); // north away from the camera
    positions.set([x, y, zw], k * 3);
    matrix.makeTranslation(x, y, zw);
    mesh.setMatrixAt(k, matrix);
    const cls = model.classes[model.class_idx[idx]];
    counts[cls] = (counts[cls] || 0) + 1;
    mesh.setColorAt(k, colour.set(LITH_COLOURS[cls] || LITH_COLOURS.UNKNOWN));
  });
  mesh.instanceColor.needsUpdate = true;
  scene.add(mesh);

  // Borehole sticks: where the model is actually constrained.
  if (model.boreholes && model.boreholes.length) {
    const pts = [];
    for (const b of model.boreholes) {
      const i = (b.x - model.origin_bng[0]) / cxy, j = (b.y - model.origin_bng[1]) / cxy;
      const x = i * cxy - width / 2, zw = -(j * cxy - depth / 2);
      const si = Math.min(nx - 1, Math.max(0, Math.floor(i))), sj = Math.min(ny - 1, Math.max(0, Math.floor(j)));
      const surf = model.surface[sj * nx + si];
      const top = (surf - z0) * VEXAG, bottom = (surf - b.depth - z0) * VEXAG;
      pts.push(new THREE.Vector3(x, top + cz * VEXAG, zw), new THREE.Vector3(x, Math.max(bottom, 0), zw));
    }
    const lines = new THREE.LineSegments(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: 0x1f2a2e, transparent: true, opacity: 0.85 })
    );
    scene.add(lines);
  }

  // A faint frame for the model box, so peeled-away space still reads as "removed".
  const frame = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(width, (zTop - z0) * VEXAG, depth)),
    new THREE.LineBasicMaterial({ color: 0x9aa09c, transparent: true, opacity: 0.6 })
  );
  frame.position.set(0, ((zTop - z0) * VEXAG) / 2, 0);
  scene.add(frame);

  camera.position.set(width * 0.85, (zTop - z0) * VEXAG * 1.6, depth * 0.9);
  controls.target.set(0, ((zTop - z0) * VEXAG) / 2, 0);
  controls.update();

  // ---------- controls: peel and certainty
  const peel = $("peel"), peelReadout = $("peel-readout");
  const entropyInput = $("entropy"), entropyReadout = $("entropy-readout");
  let peelElevation = zTop, maxEntropy = 1.0;

  function applyVisibility() {
    below.forEach((idx, k) => {
      const kz = Math.floor(idx / (nx * ny));
      const voxelTop = z0 + (kz + 1) * cz;
      const visible = voxelTop <= peelElevation + 1e-6 && model.entropy[idx] <= maxEntropy;
      if (visible) {
        matrix.makeTranslation(positions[k * 3], positions[k * 3 + 1], positions[k * 3 + 2]);
        mesh.setMatrixAt(k, matrix);
      } else {
        mesh.setMatrixAt(k, HIDDEN);
      }
    });
    mesh.instanceMatrix.needsUpdate = true;
  }

  peel.addEventListener("input", () => {
    const t = Number(peel.value) / Number(peel.max);
    peelElevation = z0 + t * (zTop - z0);
    const text = t >= 0.999 ? "surface" : `${peelElevation.toFixed(0)} m AOD`;
    peelReadout.textContent = text;
    peel.setAttribute("aria-valuetext", text);
    applyVisibility();
  });
  entropyInput.addEventListener("input", () => {
    maxEntropy = Number(entropyInput.value) / 100;
    const text = maxEntropy >= 0.999 ? "everything" : `≤ ${maxEntropy.toFixed(2)} entropy`;
    entropyReadout.textContent = text;
    entropyInput.setAttribute("aria-valuetext", text);
    applyVisibility();
  });

  // Ruler ticks in metres AOD, laid along the peel track.
  const ticks = $("ruler-ticks");
  const track = document.querySelector(".ruler-track");
  function layoutRuler() {
    ticks.innerHTML = "";
    const h = track.clientHeight;
    document.querySelector(".ruler-input").style.setProperty("--track-h", `${h}px`);
    const step = (zTop - z0) >= 60 ? 10 : 5;
    for (let e = Math.ceil(z0 / step) * step; e <= zTop; e += step) {
      const li = document.createElement("li");
      const t = (zTop - e) / (zTop - z0);
      li.style.top = `${t * 100}%`;
      li.textContent = `${e} m`;
      if (e % (step * 2) === 0) li.classList.add("major");
      ticks.appendChild(li);
    }
  }
  layoutRuler();

  // ---------- legend, from what is actually in the model
  const legend = $("legend-list");
  for (const cls of model.classes) {
    if (!counts[cls]) continue;
    const li = document.createElement("li");
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = LITH_COLOURS[cls] || LITH_COLOURS.UNKNOWN;
    const name = document.createElement("span");
    name.textContent = cls.toLowerCase().replace("_", " ");
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = counts[cls].toLocaleString("en-GB");
    li.append(sw, name, count);
    legend.appendChild(li);
  }

  // ---------- hover probe
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const probe = $("probe");
  let lastHit = -1;
  canvas.addEventListener("pointermove", (ev) => {
    const rect = canvas.getBoundingClientRect();
    pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObject(mesh, false)[0];
    if (!hit || hit.instanceId === undefined) {
      probe.hidden = true;
      lastHit = -1;
      return;
    }
    if (hit.instanceId === lastHit) return;
    lastHit = hit.instanceId;
    const idx = below[hit.instanceId];
    const i = idx % nx, j = Math.floor(idx / nx) % ny, kz = Math.floor(idx / (nx * ny));
    const cls = model.classes[model.class_idx[idx]];
    const e = model.origin_bng[0] + (i + 0.5) * cxy, nn = model.origin_bng[1] + (j + 0.5) * cxy;
    const ztop = z0 + (kz + 1) * cz;
    $("probe-class").textContent = cls.toLowerCase().replace("_", " ");
    $("probe-coords").textContent = `E ${e.toFixed(0)}  N ${nn.toFixed(0)}  ·  ${(ztop - cz).toFixed(1)}–${ztop.toFixed(1)} m AOD`;
    $("probe-certainty").textContent = `${((1 - model.entropy[idx]) * 100).toFixed(0)}%`;
    probe.hidden = false;
  });
  canvas.addEventListener("pointerleave", () => { probe.hidden = true; lastHit = -1; });

  // ---------- resize + render loop
  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w || canvas.height !== h) {
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      layoutRuler();
    }
  }
  window.addEventListener("resize", resize);
  renderer.setAnimationLoop(() => {
    resize();
    controls.update();
    renderer.render(scene, camera);
  });
}

main();
