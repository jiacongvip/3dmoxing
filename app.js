import * as THREE from './vendor/three.module.min.js';
import { OrbitControls } from './vendor/OrbitControls.js';
import { GLTFLoader } from './vendor/GLTFLoader.js';
import { DRACOLoader } from './vendor/DRACOLoader.js';

const sharedDracoLoader = new DRACOLoader();
sharedDracoLoader.setDecoderPath('./vendor/draco/');

function createGltfLoader() {
  const loader = new GLTFLoader();
  loader.setDRACOLoader(sharedDracoLoader);
  return loader;
}

if (window.location.protocol === 'file:') {
  window.location.replace('http://127.0.0.1:8765/');
}

const sceneHost = document.querySelector('#scene');
const viewerShell = document.querySelector('#viewerShell');
const loadingScreen = document.querySelector('#loadingScreen');
const loadingTitle = document.querySelector('#loadingTitle');
const loadingProgress = document.querySelector('#loadingProgress');
const loadingBar = document.querySelector('#loadingBar');
const modelInput = document.querySelector('#modelInput');
const openModelButton = document.querySelector('#openModelButton');
const modelStatus = document.querySelector('#modelStatus');
const productTitle = document.querySelector('#productTitle');
const fabricMeta = document.querySelector('#fabricMeta');
const autoRotateButton = document.querySelector('#autoRotateButton');
const resetButton = document.querySelector('#resetButton');
const captureButton = document.querySelector('#captureButton');
const fullscreenButton = document.querySelector('#fullscreenButton');
const saveButton = document.querySelector('#saveButton');
const patternUploadButton = document.querySelector('#patternUploadButton');
const patternButtonLabel = document.querySelector('#patternButtonLabel');
const patternInput = document.querySelector('#patternInput');
const patternSizeInput = document.querySelector('#patternSizeInput');
const patternSizeValue = document.querySelector('#patternSizeValue');
const patternPositionXInput = document.querySelector('#patternPositionXInput');
const patternPositionXLabel = document.querySelector('#patternPositionXLabel');
const patternPositionXValue = document.querySelector('#patternPositionXValue');
const patternPositionYInput = document.querySelector('#patternPositionYInput');
const patternPositionYValue = document.querySelector('#patternPositionYValue');
const patternRemoveButton = document.querySelector('#patternRemoveButton');
const patternPlacement = document.querySelector('#patternPlacement');
const toast = document.querySelector('#toast');
const toastText = document.querySelector('#toastText');

const FABRIC_NAMES = {
  cotton: '柔棉',
  knit: '精纺',
  performance: '速干',
};

const DEFAULT_MODEL_URL = './assets/custom-garment.glb?v=20';
const DEFAULT_PREVIEW_COLOR = '#ffffff';
const GARMENT_MASK_URL = './assets/garment-recolor-mask.png?v=20';
const DEFAULT_MODEL_NAME = '定制成衣 3D 样板';
const PLACEMENT_NAMES = {
  front: '正面',
  back: '背面',
  left_sleeve: '左袖',
  right_sleeve: '右袖',
  pants: '下装',
  pants_back: '下装背面',
};


function createEmptyPattern(placement = 'front') {
  return {
    file: null,
    dataUrl: '',
    filename: '',
    scale: 1,
    positionX: 0,
    positionY: 0,
  };
}

const state = {
  color: '#234c43',
  colorName: '松柏绿',
  fabric: 'cotton',
  currentModel: null,
  importedModel: null,
  demoModel: null,
  garmentMaterial: null,
  garmentMeshes: [],
  importedGarmentMaterials: [],
  importedSolidMaterials: [],
  importedTexturedMaterials: [],
  garmentMaskImage: null,
  garmentMaskPromise: null,
  activePatternPlacement: 'front',
  patterns: {
    front: createEmptyPattern('front'),
    back: createEmptyPattern('back'),
    left_sleeve: createEmptyPattern('left_sleeve'),
    right_sleeve: createEmptyPattern('right_sleeve'),
    pants: createEmptyPattern('pants'),
    pants_back: createEmptyPattern('pants_back'),
  },
  generationTaskId: '',
  generatedModelUrl: '',
  isGenerating: false,
  toastTimer: null,
  previewMode: false,
};

// 预览模式：商城把白胚 GLB 和各面效果图地址放在 URL 上，客户直接打开本页看效果。
// ?model=GLB地址&color=%23000000&front=正面图&back=背面图&left_sleeve=..&right_sleeve=..&title=球服&token=..
function readPreviewConfig() {
  const params = new URLSearchParams(window.location.search);
  const model = String(params.get('model') || '').trim();
  if (!/^https?:\/\//i.test(model)) return null;
  const color = String(params.get('color') || '').trim();
  const patterns = [];
    Object.keys(PLACEMENT_NAMES).forEach((placement) => {
    const imageUrl = String(params.get(placement) || '').trim();
    if (!/^https?:\/\//i.test(imageUrl)) return;
    const base = createEmptyPattern(placement);
    const extraUrl = (suffix) => {
      const value = String(params.get(placement + suffix) || '').trim();
      return /^https?:\/\//i.test(value) ? value : '';
    };
    patterns.push({
      placement,
      image_url: imageUrl,
      base_image_url: extraUrl('_base'),
      overlay_image_url: extraUrl('_overlay'),
      mask_image_url: extraUrl('_mask'),
      materials_only: params.get(placement + '_print') === '1' ? 1 : 0,
      frame: String(params.get(placement + '_frame') || '').trim(),
      scale: 1,
      position_x: base.positionX,
      position_y: base.positionY,
    });
  });
  return {
    model,
    color: /^#[0-9a-f]{6}$/i.test(color) ? color.toLowerCase() : '',
    patterns,
    title: String(params.get('title') || '').trim(),
    token: String(params.get('token') || '').trim(),
  };
}

const previewConfig = readPreviewConfig();

function apiHeaders(extra = {}) {
  const headers = { ...extra };
  if (previewConfig?.token) headers['X-Internal-Token'] = previewConfig.token;
  return headers;
}

const scene = new THREE.Scene();
scene.background = new THREE.Color('#f1f0eb');
scene.fog = new THREE.Fog('#f1f0eb', 15, 28);

const camera = new THREE.PerspectiveCamera(31, 1, 0.1, 100);
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(sceneHost.clientWidth, sceneHost.clientHeight, false);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.shadowMap.autoUpdate = false;
renderer.shadowMap.needsUpdate = true;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.04;
sceneHost.appendChild(renderer.domElement);

function refreshShadows() {
  renderer.shadowMap.needsUpdate = true;
}

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.07;
controls.enablePan = false;
controls.autoRotate = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
controls.autoRotateSpeed = 0.78;
controls.minDistance = 5.5;
controls.maxDistance = 19;
controls.minPolarAngle = Math.PI * 0.28;
controls.maxPolarAngle = Math.PI * 0.63;

const hemiLight = new THREE.HemisphereLight('#ffffff', '#9d9a8f', 2.15);
scene.add(hemiLight);

const keyLight = new THREE.DirectionalLight('#fff8ed', 3.6);
keyLight.position.set(4.2, 6.5, 5.2);
keyLight.castShadow = true;
const shadowMapSize = window.innerWidth <= 760 ? 1024 : 2048;
keyLight.shadow.mapSize.set(shadowMapSize, shadowMapSize);
keyLight.shadow.camera.left = -4;
keyLight.shadow.camera.right = 4;
keyLight.shadow.camera.top = 6;
keyLight.shadow.camera.bottom = -1;
keyLight.shadow.bias = -0.00035;
keyLight.shadow.normalBias = 0.035;
scene.add(keyLight);

const rimLight = new THREE.DirectionalLight('#c5d9d3', 2.15);
rimLight.position.set(-4.5, 3.8, -3.5);
scene.add(rimLight);

const fillLight = new THREE.DirectionalLight('#e3c89f', 1.05);
fillLight.position.set(-3.5, 2.5, 5.5);
scene.add(fillLight);

const floor = new THREE.Mesh(
  new THREE.PlaneGeometry(30, 30),
  new THREE.MeshStandardMaterial({ color: '#e7e6e0', roughness: 0.96, metalness: 0 }),
);
floor.rotation.x = -Math.PI / 2;
floor.receiveShadow = true;
scene.add(floor);

const pedestal = new THREE.Mesh(
  new THREE.CylinderGeometry(1.42, 1.52, 0.12, 96),
  new THREE.MeshStandardMaterial({ color: '#d8d7d1', roughness: 0.74, metalness: 0.04 }),
);
pedestal.position.y = 0.06;
pedestal.receiveShadow = true;
pedestal.castShadow = true;
scene.add(pedestal);

function makeFabricTexture(color, fabric) {
  const size = 256;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext('2d');
  const base = new THREE.Color(color);
  context.fillStyle = `#${base.getHexString()}`;
  context.fillRect(0, 0, size, size);

  if (fabric === 'cotton') {
    for (let index = 0; index < 2600; index += 1) {
      const alpha = 0.018 + Math.random() * 0.035;
      const light = Math.random() > 0.52;
      context.fillStyle = light ? `rgba(255,255,255,${alpha})` : `rgba(0,0,0,${alpha})`;
      context.fillRect(Math.random() * size, Math.random() * size, 1, 1);
    }
  }

  if (fabric === 'knit') {
    context.lineWidth = 1;
    for (let offset = -size; offset < size * 2; offset += 7) {
      context.strokeStyle = 'rgba(255,255,255,0.065)';
      context.beginPath();
      context.moveTo(offset, 0);
      context.lineTo(offset - size, size);
      context.stroke();
      context.strokeStyle = 'rgba(0,0,0,0.055)';
      context.beginPath();
      context.moveTo(offset + 3, 0);
      context.lineTo(offset - size + 3, size);
      context.stroke();
    }
  }

  if (fabric === 'performance') {
    for (let y = 3; y < size; y += 7) {
      for (let x = 3; x < size; x += 7) {
        context.fillStyle = 'rgba(255,255,255,0.075)';
        context.beginPath();
        context.arc(x, y, 1.15, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = 'rgba(0,0,0,0.05)';
        context.fillRect(x + 2, y + 2, 1, 1);
      }
    }
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(3, 3);
  texture.anisotropy = renderer.capabilities.getMaxAnisotropy();
  return texture;
}

function createMaterial(color, fabric) {
  const properties = {
    color: '#ffffff',
    map: makeFabricTexture(color, fabric),
    roughness: fabric === 'performance' ? 0.58 : 0.88,
    metalness: 0,
    clearcoat: fabric === 'performance' ? 0.12 : 0,
    clearcoatRoughness: 0.74,
    sheen: fabric === 'knit' ? 0.42 : 0.16,
    sheenColor: new THREE.Color(color).offsetHSL(0, -0.08, 0.08),
    sheenRoughness: 0.82,
  };
  return new THREE.MeshPhysicalMaterial(properties);
}

function setShadow(mesh) {
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

function createPoloModel() {
  const group = new THREE.Group();
  group.name = 'DemoPoloModel';

  const skinMaterial = new THREE.MeshPhysicalMaterial({
    color: '#c99675',
    roughness: 0.72,
    metalness: 0,
    sheen: 0.08,
  });
  const hairMaterial = new THREE.MeshStandardMaterial({ color: '#25231f', roughness: 0.88 });
  const trouserMaterial = new THREE.MeshPhysicalMaterial({
    color: '#5e6462',
    roughness: 0.9,
    sheen: 0.2,
    sheenColor: new THREE.Color('#989e9b'),
  });
  const shoeMaterial = new THREE.MeshPhysicalMaterial({
    color: '#eeeae0',
    roughness: 0.58,
    clearcoat: 0.12,
  });
  const goldMaterial = new THREE.MeshPhysicalMaterial({
    color: '#c89542',
    roughness: 0.38,
    metalness: 0.52,
  });

  state.garmentMaterial = createMaterial(state.color, state.fabric);

  const head = setShadow(new THREE.Mesh(new THREE.SphereGeometry(0.34, 48, 36), skinMaterial));
  head.position.set(0, 4.02, 0);
  head.scale.set(0.86, 1.08, 0.92);
  group.add(head);

  const hair = setShadow(
    new THREE.Mesh(new THREE.SphereGeometry(0.346, 48, 28, 0, Math.PI * 2, 0, Math.PI * 0.48), hairMaterial),
  );
  hair.position.set(0, 4.09, -0.012);
  hair.scale.set(0.88, 1.02, 0.94);
  group.add(hair);

  const neck = setShadow(new THREE.Mesh(new THREE.CylinderGeometry(0.135, 0.155, 0.34, 32), skinMaterial));
  neck.position.y = 3.58;
  group.add(neck);

  const shirtBody = setShadow(
    new THREE.Mesh(new THREE.CylinderGeometry(0.73, 0.62, 1.46, 56, 8), state.garmentMaterial),
  );
  shirtBody.name = 'shirt_body';
  shirtBody.position.y = 2.72;
  shirtBody.scale.z = 0.59;
  group.add(shirtBody);
  state.garmentMeshes.push(shirtBody);

  [-1, 1].forEach((side) => {
    const sleeve = setShadow(
      new THREE.Mesh(new THREE.CylinderGeometry(0.28, 0.22, 0.57, 40, 4), state.garmentMaterial),
    );
    sleeve.name = side < 0 ? 'shirt_sleeve_left' : 'shirt_sleeve_right';
    sleeve.position.set(side * 0.77, 3.0, 0);
    sleeve.rotation.z = side * -0.2;
    sleeve.scale.z = 0.92;
    group.add(sleeve);
    state.garmentMeshes.push(sleeve);

    const arm = setShadow(new THREE.Mesh(new THREE.CapsuleGeometry(0.145, 0.91, 12, 24), skinMaterial));
    arm.position.set(side * 0.92, 2.23, 0);
    arm.rotation.z = side * -0.055;
    group.add(arm);

    const hand = setShadow(new THREE.Mesh(new THREE.SphereGeometry(0.16, 30, 24), skinMaterial));
    hand.position.set(side * 0.97, 1.61, 0.01);
    hand.scale.set(0.8, 1.2, 0.72);
    group.add(hand);
  });

  const collarShape = new THREE.Shape();
  collarShape.moveTo(-0.18, 0.13);
  collarShape.lineTo(0.03, 0.11);
  collarShape.lineTo(0.1, -0.22);
  collarShape.lineTo(-0.13, -0.11);
  collarShape.closePath();
  const collarGeometry = new THREE.ShapeGeometry(collarShape);

  [-1, 1].forEach((side) => {
    const collar = setShadow(new THREE.Mesh(collarGeometry, state.garmentMaterial));
    collar.name = side < 0 ? 'shirt_collar_left' : 'shirt_collar_right';
    collar.position.set(side * 0.08, 3.34, 0.426);
    collar.scale.x = side;
    collar.rotation.z = side * -0.06;
    group.add(collar);
    state.garmentMeshes.push(collar);
  });

  const placket = setShadow(
    new THREE.Mesh(new THREE.BoxGeometry(0.075, 0.43, 0.035), state.garmentMaterial),
  );
  placket.name = 'shirt_placket';
  placket.position.set(0, 3.14, 0.43);
  group.add(placket);
  state.garmentMeshes.push(placket);

  [3.27, 3.14, 3.01].forEach((y) => {
    const button = new THREE.Mesh(
      new THREE.CylinderGeometry(0.025, 0.025, 0.018, 24),
      new THREE.MeshStandardMaterial({ color: '#151817', roughness: 0.48 }),
    );
    button.rotation.x = Math.PI / 2;
    button.position.set(0, y, 0.46);
    group.add(button);
  });

  const badge = setShadow(new THREE.Mesh(new THREE.CylinderGeometry(0.095, 0.095, 0.018, 48), goldMaterial));
  badge.rotation.x = Math.PI / 2;
  badge.position.set(-0.32, 2.98, 0.425);
  group.add(badge);

  const badgeInset = new THREE.Mesh(
    new THREE.TorusGeometry(0.056, 0.009, 14, 40),
    new THREE.MeshStandardMaterial({ color: '#234c43', roughness: 0.65 }),
  );
  badgeInset.position.set(-0.32, 2.98, 0.441);
  group.add(badgeInset);

  const hip = setShadow(new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.49, 0.32, 48), trouserMaterial));
  hip.position.y = 1.86;
  hip.scale.z = 0.64;
  group.add(hip);

  [-1, 1].forEach((side) => {
    const leg = setShadow(new THREE.Mesh(new THREE.CylinderGeometry(0.245, 0.19, 1.55, 36), trouserMaterial));
    leg.position.set(side * 0.26, 1.02, 0);
    leg.scale.z = 0.82;
    group.add(leg);

    const shoe = setShadow(new THREE.Mesh(new THREE.CapsuleGeometry(0.18, 0.42, 10, 24), shoeMaterial));
    shoe.rotation.x = Math.PI / 2;
    shoe.position.set(side * 0.26, 0.28, 0.13);
    shoe.scale.set(1.02, 1, 0.62);
    group.add(shoe);
  });

  group.position.y = 0.12;
  group.rotation.y = -0.12;
  return group;
}

function hexToSrgb(hex) {
  const value = String(hex || '').replace('#', '');
  return [
    parseInt(value.slice(0, 2), 16) / 255,
    parseInt(value.slice(2, 4), 16) / 255,
    parseInt(value.slice(4, 6), 16) / 255,
  ];
}

function channelLuma(red, green, blue) {
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.crossOrigin = 'anonymous';
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('图片载入失败'));
    image.src = url;
  });
}

function ensureGarmentMask() {
  if (state.garmentMaskImage) {
    return Promise.resolve(state.garmentMaskImage);
  }
  if (!state.garmentMaskPromise) {
    state.garmentMaskPromise = loadImage(GARMENT_MASK_URL)
      .then((image) => {
        state.garmentMaskImage = image;
        return image;
      })
      .catch(() => null);
  }
  return state.garmentMaskPromise;
}

function disposeTintedMap(material) {
  const tinted = material.userData.tintedMap;
  if (tinted && tinted !== material.userData.originalMap) {
    tinted.dispose();
  }
  material.userData.tintedMap = null;
}

function applyAlbedoTint(material, maskImage, hex) {
  const original = material.userData.originalMap || material.map;
  const source = original?.image;
  if (!original || !source) {
    return;
  }
  material.userData.originalMap = original;

  const width = Math.min(2048, source.naturalWidth || source.width || 1024);
  const height = Math.min(2048, source.naturalHeight || source.height || 1024);
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d', { willReadFrequently: true });
  context.drawImage(source, 0, 0, width, height);

  let maskPixels = null;
  if (maskImage) {
    const maskCanvas = document.createElement('canvas');
    maskCanvas.width = width;
    maskCanvas.height = height;
    const maskContext = maskCanvas.getContext('2d', { willReadFrequently: true });
    maskContext.drawImage(maskImage, 0, 0, width, height);
    maskPixels = maskContext.getImageData(0, 0, width, height).data;
  }

  const imageData = context.getImageData(0, 0, width, height);
  const [tintRed, tintGreen, tintBlue] = hexToSrgb(hex);
  const tintLuma = Math.max(channelLuma(tintRed, tintGreen, tintBlue), 0.06);
  const pixels = imageData.data;
  for (let index = 0; index < pixels.length; index += 4) {
    const red = pixels[index] / 255;
    const green = pixels[index + 1] / 255;
    const blue = pixels[index + 2] / 255;
    const luma = channelLuma(red, green, blue);
    const chroma = Math.max(red, green, blue) - Math.min(red, green, blue);
    const isSkin = red > 0.14 && red > green * 1.1 && green > blue * 1.02 && red - blue > 0.07;
    const isBlackTrim = luma < 0.058 && chroma < 0.06;
    const isLogo = blue > red * 1.08 && blue > green * 1.02 && chroma > 0.12;
    if (isSkin || isBlackTrim || isLogo) {
      continue;
    }

    const region = maskPixels ? maskPixels[index] / 255 : 0;
    const isGreenCloth = green > red * 1.02 && green > blue * 1.02 && luma > 0.04 && luma < 0.36;
    const isNeutralCloth = luma > 0.046 && luma < 0.32 && chroma < 0.14;
    let amount = Math.max(region, isGreenCloth ? 0.92 : 0, isNeutralCloth ? 0.72 : 0);
    if (chroma < 0.06 && luma < 0.075) {
      amount *= Math.max(0, (luma - 0.034) / 0.041);
    }
    if (amount < 0.04) {
      continue;
    }

    const scale = luma / tintLuma;
    const mix = 1 - amount;
    pixels[index] = Math.round((red * mix + tintRed * scale * amount) * 255);
    pixels[index + 1] = Math.round((green * mix + tintGreen * scale * amount) * 255);
    pixels[index + 2] = Math.round((blue * mix + tintBlue * scale * amount) * 255);
  }
  context.putImageData(imageData, 0, 0);

  disposeTintedMap(material);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.flipY = false;
  texture.wrapS = original.wrapS;
  texture.wrapT = original.wrapT;
  texture.needsUpdate = true;
  material.userData.tintedMap = texture;
  material.map = texture;
  material.color.set('#ffffff');
  material.needsUpdate = true;
}

function updateFabric() {
  // 预览模式下颜色已由 Blender 写进 GLB，不再按演示模型的遮罩二次染色。
  if (state.previewMode) return;
  if (state.garmentMaterial) {
    state.garmentMaterial.map?.dispose();
    state.garmentMaterial.map = makeFabricTexture(state.color, state.fabric);
    state.garmentMaterial.roughness = state.fabric === 'performance' ? 0.58 : 0.88;
    state.garmentMaterial.clearcoat = state.fabric === 'performance' ? 0.12 : 0;
    state.garmentMaterial.sheen = state.fabric === 'knit' ? 0.42 : 0.16;
    state.garmentMaterial.sheenColor.set(state.color).offsetHSL(0, -0.08, 0.08);
    state.garmentMaterial.needsUpdate = true;
  }

  const roughness = state.fabric === 'performance' ? 0.58 : 0.88;
  state.importedSolidMaterials.forEach((material) => {
    material.color.set(state.color);
    material.roughness = roughness;
    material.needsUpdate = true;
  });

  state.importedTexturedMaterials.forEach((material) => {
    material.roughness = roughness;
    applyAlbedoTint(material, state.garmentMaskImage, state.color);
  });
}

function configureCamera() {
  const isMobile = window.innerWidth <= 760;
  camera.position.set(0, isMobile ? 2.1 : 2.34, isMobile ? 15 : 11.8);
  controls.target.set(0, isMobile ? 1.98 : 2.2, 0);
  controls.update();
}

function resetCamera() {
  configureCamera();
  controls.autoRotate = autoRotateButton.getAttribute('aria-pressed') === 'true';
}

function clearPatternSelection() {
  Object.keys(state.patterns).forEach((placement) => {
    state.patterns[placement] = createEmptyPattern(placement);
  });
  state.activePatternPlacement = 'front';
  state.generationTaskId = '';
  document.querySelectorAll('[data-placement]').forEach((button) => {
    const selected = button.dataset.placement === 'front';
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-checked', String(selected));
  });
  syncPatternControls();
}

function selectedPatterns() {
  return Object.entries(state.patterns)
    .filter(([, pattern]) => pattern.file && pattern.dataUrl)
    .map(([placement, pattern]) => ({ placement, ...pattern }));
}

function formatAxisValue(value, negativeLabel, positiveLabel) {
  const percentage = Math.round(Math.abs(value) * 100);
  if (percentage === 0) return '居中';
  return `${value < 0 ? negativeLabel : positiveLabel} ${percentage}%`;
}

function syncPatternControls() {
  const pattern = state.patterns[state.activePatternPlacement];
  const isSleeve = state.activePatternPlacement.endsWith('_sleeve');
  const selected = Boolean(pattern.file);
  patternSizeInput.value = String(pattern.scale);
  patternPositionXInput.value = String(pattern.positionX);
  patternPositionYInput.value = String(pattern.positionY);
  patternSizeValue.textContent = `${Math.round(pattern.scale * 100)}%`;
  patternPositionXLabel.textContent = isSleeve ? '前后' : '左右';
  patternPositionXValue.textContent = formatAxisValue(pattern.positionX, isSleeve ? '后' : '左', isSleeve ? '前' : '右');
  patternPositionYValue.textContent = formatAxisValue(pattern.positionY, '下', '上');
  patternSizeInput.disabled = state.isGenerating || !selected;
  patternPositionXInput.disabled = state.isGenerating || !selected;
  patternPositionYInput.disabled = state.isGenerating || !selected;
  patternRemoveButton.disabled = state.isGenerating || !selected;
  patternUploadButton.disabled = state.isGenerating;
  document.querySelectorAll('[data-placement]').forEach((button) => {
    button.disabled = state.isGenerating;
  });
  patternButtonLabel.textContent = selected ? '更换图案' : '添加图案';
  saveButton.disabled = state.isGenerating || selectedPatterns().length === 0;
}

function loadPatternFile(file) {
  if (!file) return;
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
    showToast('请选择 PNG、JPG 或 WebP 图片');
    return;
  }
  if (file.size > 12 * 1024 * 1024) {
    showToast('图案不能超过 12MB');
    return;
  }

  const placement = state.activePatternPlacement;
  const reader = new FileReader();
  reader.addEventListener('load', () => {
    if (typeof reader.result !== 'string') {
      showToast('图片读取失败');
      return;
    }
    state.patterns[placement] = {
      ...state.patterns[placement],
      file,
      dataUrl: reader.result,
      filename: file.name,
    };
    syncPatternControls();
    showToast(`${PLACEMENT_NAMES[placement]}图案已选择`);
  });
  reader.addEventListener('error', () => showToast('图片读取失败'));
  reader.readAsDataURL(file);
}

function removePattern(showMessage = true) {
  const placement = state.activePatternPlacement;
  if (!state.patterns[placement].file) return;
  const restoreBaseModel = Boolean(state.generatedModelUrl);
  state.patterns[placement] = createEmptyPattern(placement);
  state.generatedModelUrl = '';
  syncPatternControls();
  if (restoreBaseModel) loadDefaultModel();
  if (showMessage) {
    showToast(`${PLACEMENT_NAMES[placement]}图案已删除${restoreBaseModel ? '，请重新生成' : ''}`);
  }
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function pollGenerationTask(taskId, options = {}) {
  const { silent = false } = options;
  for (let attempt = 0; attempt < 300; attempt += 1) {
    const response = await fetch(`/api/tasks/${taskId}`, { cache: 'no-store', headers: apiHeaders() });
    const payload = await response.json();
    if (!response.ok || payload.code !== 200) throw new Error(payload.msg || '生成任务查询失败');
    const task = payload.data;
    if (silent) {
      modelStatus.innerHTML = `<span class="status-dot" aria-hidden="true"></span>${task.message || '正在贴合图案...'}`;
    } else {
      setLoading(true, 'Blender 正在生成 3D 样衣', task.message || '处理服装曲面', task.progress || 12);
    }
    if (task.status === 'completed') return task;
    if (task.status === 'failed') throw new Error(task.error || 'Blender 生成失败');
    await wait(attempt < 25 ? 300 : 600);
  }
  throw new Error('Blender 生成超时');
}

function loadGltfUrl(url, onProgress) {
  return new Promise((resolve, reject) => {
    const loader = createGltfLoader();
    loader.load(
      url,
      (gltf) => resolve(gltf),
      onProgress,
      () => reject(new Error('GLB 载入失败')),
    );
  });
}

function prepareDecalMaterials(root) {
  root.traverse((node) => {
    if (!node.isMesh) return;
    node.castShadow = false;
    node.receiveShadow = false;
    node.renderOrder = 2;
    const nodeMaterials = Array.isArray(node.material) ? node.material : [node.material];
    nodeMaterials.forEach((material) => {
      if (!material?.isMaterial) return;
      if (material.map) material.map.colorSpace = THREE.SRGBColorSpace;
      material.depthWrite = false;
      material.polygonOffset = true;
      material.polygonOffsetFactor = -2;
      material.polygonOffsetUnits = -2;
    });
  });
}

function loadGeneratedModel(url, placements) {
  return new Promise((resolve, reject) => {
    const loader = createGltfLoader();
    loader.load(
      url,
      (gltf) => {
        try {
          const displayName = placements.length > 1
            ? 'Blender 多位置定制样衣'
            : `Blender ${PLACEMENT_NAMES[placements[0]]}定制样衣`;
          replaceWithImportedModel(gltf.scene, url, displayName, {
            statusText: 'Blender 已生成',
            toastMessage: '图案已通过 Blender 贴合到衣服',
          });
          state.generatedModelUrl = url;
          resolve();
        } catch (error) {
          reject(error);
        }
      },
      (event) => {
        if (!event.total) return;
        const percent = Math.round((event.loaded / event.total) * 100);
        setLoading(true, '正在载入定制 GLB', `已完成 ${percent}%`, percent);
      },
      () => reject(new Error('定制 GLB 载入失败')),
    );
  });
}

async function generateBlenderModel() {
  const patterns = selectedPatterns();
  if (patterns.length === 0 || state.isGenerating) {
    if (patterns.length === 0) showToast('请先添加正面或背面图案');
    return;
  }

  state.isGenerating = true;
  syncPatternControls();
  setAutoRotate(false);
  setLoading(true, '正在提交 Blender 任务', '上传图案参数', 8);

  try {
    const response = await fetch('/api/generate', {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        patterns: patterns.map((pattern) => ({
          placement: pattern.placement,
          image_data: pattern.dataUrl,
          filename: pattern.filename,
          scale: pattern.scale,
          position_x: pattern.positionX,
          position_y: pattern.positionY,
        })),
        color: state.color,
      }),
    });
    const payload = await response.json();
    if (!response.ok || payload.code !== 202) throw new Error(payload.msg || 'Blender 任务创建失败');
    state.generationTaskId = payload.data.task_id;
    const task = await pollGenerationTask(state.generationTaskId);
    await loadGeneratedModel(task.model_url, patterns.map((pattern) => pattern.placement));
    localStorage.setItem(
      'clothing-3d-demo-selection',
      JSON.stringify({
        color: state.color,
        colorName: state.colorName,
        fabric: state.fabric,
        patterns: patterns.map((pattern) => ({
          placement: pattern.placement,
          filename: pattern.filename,
          scale: pattern.scale,
          positionX: pattern.positionX,
          positionY: pattern.positionY,
        })),
        modelUrl: task.model_url,
      }),
    );
  } catch (error) {
    setLoading(false);
    showToast(error.message || 'Blender 生成失败');
  } finally {
    state.isGenerating = false;
    syncPatternControls();
  }
}

function loadPreviewBaseModel(config, options = {}) {
  const {
    statusText = '白胚模型',
    toastMessage = '已载入白胚，暂无可贴合的效果图',
    hideLoadingOnReady = true,
  } = options;
  return loadGltfUrl(
    `/api/model?url=${encodeURIComponent(config.model)}`,
    (event) => {
      if (!event.total) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      setLoading(true, '正在载入 3D 模型', `已完成 ${percent}%`, Math.max(10, Math.min(95, percent)));
    },
  ).then((gltf) => {
    const wrapper = new THREE.Group();
    wrapper.name = 'PreviewGarmentWrapper';
    wrapper.add(gltf.scene);
    replaceWithImportedModel(wrapper, config.model, config.title || '定制样衣', {
      statusText,
      toastMessage,
      keepLoading: !hideLoadingOnReady,
    });
    return wrapper;
  });
}

async function generatePreviewDecals(config, isBaseReady) {
  const response = await fetch('/api/generate', {
    method: 'POST',
    headers: apiHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      base_model_url: config.model,
      color: config.color || DEFAULT_PREVIEW_COLOR,
      patterns: config.patterns,
      // 纯素材图靠透明通道；有白胚底图时仍做衣身对齐，避免把小 Logo 拉满全身
      fit_garment: true,
      decals_only: true,
    }),
  });
  const payload = await response.json();
  if (!response.ok || payload.code !== 202) throw new Error(payload.msg || '贴合任务创建失败');
  let decalsUrl = payload.data?.cached ? (payload.data.decals_url || payload.data.model_url || '') : '';
  if (!decalsUrl) {
    const task = await pollGenerationTask(payload.data.task_id, { silent: isBaseReady() });
    decalsUrl = task.decals_url || task.model_url;
  }
  if (!decalsUrl) throw new Error('未返回 3D 贴图地址');
  const gltf = await loadGltfUrl(decalsUrl);
  prepareDecalMaterials(gltf.scene);
  return { scene: gltf.scene, url: decalsUrl };
}

async function runPreviewMode(config) {
  state.previewMode = true;
  document.body.classList.add('is-preview');
  if (config.color) state.color = config.color;
  if (config.title) productTitle.textContent = config.title;
  setAutoRotate(true);

  if (config.patterns.length === 0) {
    setLoading(true, '正在载入白胚', '读取模型数据', 8);
    try {
      await loadPreviewBaseModel(config);
    } catch (error) {
      setLoading(false);
      showToast(error.message || '白胚载入失败');
    }
    return;
  }

  state.isGenerating = true;
  setLoading(true, '正在载入 3D 效果', '并行加载模型与图案', 10);

  let baseWrapper = null;
  let pendingDecals = null;
  let decalsFinished = false;

  const attachDecals = (decals) => {
    if (!baseWrapper || !decals) return;
    baseWrapper.add(decals.scene);
    state.generatedModelUrl = decals.url;
    refreshShadows();
    modelStatus.innerHTML = '<span class="status-dot" aria-hidden="true"></span>3D 效果';
    document.body.dataset.previewReady = '1';
    setLoading(false);
  };

  // 并行：一边立刻加载缓存的轻量白胚（秒出画面），一边请求仅几百 KB 的贴片 GLB
  const baseTask = loadPreviewBaseModel(config, {
    statusText: '正在贴合图案...',
    toastMessage: '',
    hideLoadingOnReady: true,
  })
    .then((wrapper) => {
      baseWrapper = wrapper;
      if (pendingDecals) {
        attachDecals(pendingDecals);
        pendingDecals = null;
      } else if (decalsFinished) {
        modelStatus.innerHTML = '<span class="status-dot" aria-hidden="true"></span>白胚模型';
      }
      return wrapper;
    })
    .catch((error) => {
      setLoading(false);
      showToast(error.message || '白胚载入失败');
      return null;
    });

  const decalsTask = generatePreviewDecals(config, () => Boolean(baseWrapper))
    .then((decals) => {
      decalsFinished = true;
      if (baseWrapper) {
        attachDecals(decals);
      } else {
        pendingDecals = decals;
      }
    })
    .catch((error) => {
      decalsFinished = true;
      if (baseWrapper) {
        modelStatus.innerHTML = '<span class="status-dot" aria-hidden="true"></span>白胚模型';
      }
      showToast(error.message || '图案贴合失败，已展示白胚');
    });

  try {
    await Promise.all([baseTask, decalsTask]);
  } finally {
    state.isGenerating = false;
    setLoading(false);
  }
}

function setLoading(visible, title = '正在构建 3D 样衣', detail = '初始化场景', percent = 28) {
  loadingTitle.textContent = title;
  loadingProgress.textContent = detail;
  loadingBar.style.width = `${Math.max(4, Math.min(percent, 100))}%`;
  loadingScreen.classList.toggle('is-hidden', !visible);
}

function showToast(message) {
  toastText.textContent = message;
  toast.classList.add('is-visible');
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => toast.classList.remove('is-visible'), 2200);
}

function collectImportedGarmentMaterials(root) {
  const solidPattern = /custom_garment_color|side_fill/i;
  const decalPattern = /custom_.*_pattern|decal|projected/i;
  const solids = new Set();
  const textured = new Set();
  root.traverse((node) => {
    if (!node.isMesh) return;
    const nodeName = node.name || '';
    const isSideFill = /side_fill/i.test(nodeName);
    const isDecal = decalPattern.test(nodeName);
    const nodeMaterials = Array.isArray(node.material) ? node.material : [node.material];
    nodeMaterials.forEach((material) => {
      if (!material?.isMaterial) return;
      const materialName = material.name || '';
      if (isDecal || decalPattern.test(materialName)) return;
      if (isSideFill || solidPattern.test(materialName) || !material.map) {
        if (isSideFill || solidPattern.test(materialName)) {
          solids.add(material);
        }
        return;
      }
      textured.add(material);
    });
  });
  state.importedSolidMaterials = [...solids];
  state.importedTexturedMaterials = [...textured];
  state.importedGarmentMaterials = [...solids, ...textured];
  window.__clothingDebug = {
    solids: state.importedSolidMaterials.map((material) => material.name),
    textured: state.importedTexturedMaterials.map((material) => material.name),
    hasMask: Boolean(state.garmentMaskImage),
    color: state.color,
  };
}

function normalizeModel(root) {
  root.updateMatrixWorld(true);
  const initialBox = new THREE.Box3().setFromObject(root);
  const initialSize = initialBox.getSize(new THREE.Vector3());
  if (!Number.isFinite(initialSize.y) || initialSize.y <= 0) {
    throw new Error('模型尺寸无效');
  }

  const scale = 4.35 / initialSize.y;
  root.scale.multiplyScalar(scale);
  root.updateMatrixWorld(true);

  const box = new THREE.Box3().setFromObject(root);
  const center = box.getCenter(new THREE.Vector3());
  root.position.x -= center.x;
  root.position.y -= box.min.y - 0.12;
  root.position.z -= center.z;

  root.traverse((node) => {
    if (!node.isMesh) return;
    const isSideFill = /side_fill/i.test(node.name || '');
    if (isSideFill) {
      node.visible = false;
      return;
    }
    node.castShadow = !isSideFill;
    node.receiveShadow = !isSideFill;
    const nodeMaterials = Array.isArray(node.material) ? node.material : [node.material];
    nodeMaterials.forEach((material) => {
      if (!material?.isMaterial) return;
      if (material.map) material.map.colorSpace = THREE.SRGBColorSpace;
      if (isSideFill || /custom_garment_color/i.test(material.name || '')) {
        material.side = THREE.DoubleSide;
      }
      const materialName = material.name || '';
      if (/^CUSTOM_.*_PATTERN_MATERIAL$/i.test(materialName)) {
        material.depthWrite = false;
        material.polygonOffset = true;
        material.polygonOffsetFactor = -2;
        material.polygonOffsetUnits = -2;
        node.renderOrder = 2;
      }
    });
  });
}

function replaceWithImportedModel(root, filename, displayName = '', options = {}) {
  normalizeModel(root);
  collectImportedGarmentMaterials(root);

  if (state.importedModel) scene.remove(state.importedModel);
  state.importedModel = root;
  state.demoModel.visible = false;
  state.currentModel = root;
  scene.add(root);

  const cleanName = displayName || filename.replace(/\.(glb|gltf)$/i, '');
  productTitle.textContent = cleanName || '自定义服装模型';
  modelStatus.innerHTML = `<span class="status-dot" aria-hidden="true"></span>${options.statusText || '自定义模型'}`;
  resetCamera();
  refreshShadows();
  if (state.previewMode) {
    if (!options.keepLoading) setLoading(false);
    document.body.dataset.previewReady = '1';
    if (options.toastMessage) showToast(options.toastMessage);
    return;
  }
  setLoading(true, '正在上色', '按原图换色，保留黑边', 92);
  ensureGarmentMask()
    .then(() => {
      updateFabric();
      refreshShadows();
      setLoading(false);
      document.body.dataset.previewReady = '1';
      showToast(
        options.toastMessage || (state.importedGarmentMaterials.length ? '模型已载入，可继续调整材质' : '模型已载入'),
      );
    })
    .catch(() => {
      updateFabric();
      refreshShadows();
      setLoading(false);
      document.body.dataset.previewReady = '1';
    });
}

function loadDefaultModel() {
  setLoading(true, '正在载入定制样衣', '读取模型数据', 8);
  const loader = createGltfLoader();
  loader.load(
    DEFAULT_MODEL_URL,
    (gltf) => {
      try {
        replaceWithImportedModel(gltf.scene, DEFAULT_MODEL_URL, DEFAULT_MODEL_NAME);
      } catch (error) {
        setLoading(false);
        showToast(error.message || '默认模型解析失败，已显示模拟模型');
      }
    },
    (event) => {
      if (!event.total) {
        setLoading(true, '正在载入定制样衣', '读取模型数据', 34);
        return;
      }
      const percent = Math.round((event.loaded / event.total) * 100);
      setLoading(true, '正在载入定制样衣', `已完成 ${percent}%`, percent);
    },
    () => {
      setLoading(false);
      showToast('默认模型载入失败，已显示模拟模型');
    },
  );
}

function loadLocalModel(file) {
  if (!file) return;
  if (!/\.glb$/i.test(file.name)) {
    showToast('请使用包含贴图的 GLB 文件');
    return;
  }

  clearPatternSelection();
  state.generatedModelUrl = '';
  setLoading(true, '正在载入 3D 模型', '读取模型数据', 12);
  const objectUrl = URL.createObjectURL(file);
  const loader = createGltfLoader();
  loader.load(
    objectUrl,
    (gltf) => {
      URL.revokeObjectURL(objectUrl);
      try {
        replaceWithImportedModel(gltf.scene, file.name);
      } catch (error) {
        setLoading(false);
        showToast(error.message || '模型解析失败');
      }
    },
    (event) => {
      if (!event.total) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      setLoading(true, '正在载入 3D 模型', `已完成 ${percent}%`, percent);
    },
    () => {
      URL.revokeObjectURL(objectUrl);
      setLoading(false);
      showToast('模型载入失败，请检查 GLB 文件');
    },
  );
}

function setAutoRotate(enabled) {
  controls.autoRotate = enabled;
  autoRotateButton.classList.toggle('is-active', enabled);
  autoRotateButton.setAttribute('aria-pressed', String(enabled));
  autoRotateButton.setAttribute('aria-label', enabled ? '暂停自动旋转' : '开启自动旋转');
}

function captureView() {
  renderer.render(scene, camera);
  renderer.domElement.toBlob((blob) => {
    if (!blob) {
      showToast('当前浏览器无法保存图片');
      return;
    }
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(blob);
    anchor.download = `3d-clothing-${Date.now()}.png`;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(anchor.href), 500);
    showToast('当前视图已保存');
  }, 'image/png');
}

async function toggleFullscreen() {
  try {
    if (!document.fullscreenElement) {
      await viewerShell.requestFullscreen();
    } else {
      await document.exitFullscreen();
    }
  } catch {
    showToast('当前浏览器不支持全屏');
  }
}

function handleResize() {
  const width = sceneHost.clientWidth;
  const height = sceneHost.clientHeight;
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  renderer.setSize(width, height, false);
  configureCamera();
}

document.querySelector('#colorOptions').addEventListener('click', (event) => {
  const swatch = event.target.closest('[data-color]');
  if (!swatch) return;
  document.querySelectorAll('.color-swatch').forEach((item) => {
    const selected = item === swatch;
    item.classList.toggle('is-selected', selected);
    item.setAttribute('aria-checked', String(selected));
  });
  state.color = swatch.dataset.color;
  state.colorName = swatch.dataset.name;
  updateFabric();
  if (state.importedModel && state.importedTexturedMaterials.length === 0 && state.importedSolidMaterials.length === 0) {
    showToast('当前模型未找到可换色的服装材质');
  }
});

document.querySelector('#fabricOptions').addEventListener('click', (event) => {
  const segment = event.target.closest('[data-fabric]');
  if (!segment) return;
  document.querySelectorAll('.segment').forEach((item) => {
    const selected = item === segment;
    item.classList.toggle('is-selected', selected);
    item.setAttribute('aria-checked', String(selected));
  });
  state.fabric = segment.dataset.fabric;
  fabricMeta.textContent = FABRIC_NAMES[state.fabric];
  updateFabric();
});

openModelButton.addEventListener('click', () => modelInput.click());
modelInput.addEventListener('change', () => {
  loadLocalModel(modelInput.files?.[0]);
  modelInput.value = '';
});
patternUploadButton.addEventListener('click', () => patternInput.click());
patternInput.addEventListener('change', () => {
  loadPatternFile(patternInput.files?.[0]);
  patternInput.value = '';
});
patternPlacement.addEventListener('click', (event) => {
  const button = event.target.closest('[data-placement]');
  if (!button || button.disabled) return;
  state.activePatternPlacement = button.dataset.placement;
  document.querySelectorAll('[data-placement]').forEach((item) => {
    const selected = item === button;
    item.classList.toggle('is-selected', selected);
    item.setAttribute('aria-checked', String(selected));
  });
  syncPatternControls();
});
patternSizeInput.addEventListener('input', () => {
  state.patterns[state.activePatternPlacement].scale = Number(patternSizeInput.value);
  patternSizeValue.textContent = `${Math.round(Number(patternSizeInput.value) * 100)}%`;
});
patternPositionXInput.addEventListener('input', () => {
  const value = Number(patternPositionXInput.value);
  const isSleeve = state.activePatternPlacement.endsWith('_sleeve');
  state.patterns[state.activePatternPlacement].positionX = value;
  patternPositionXValue.textContent = formatAxisValue(value, isSleeve ? '后' : '左', isSleeve ? '前' : '右');
});
patternPositionYInput.addEventListener('input', () => {
  const value = Number(patternPositionYInput.value);
  state.patterns[state.activePatternPlacement].positionY = value;
  patternPositionYValue.textContent = formatAxisValue(value, '下', '上');
});
patternRemoveButton.addEventListener('click', () => removePattern(true));
autoRotateButton.addEventListener('click', () => setAutoRotate(!controls.autoRotate));
resetButton.addEventListener('click', resetCamera);
captureButton.addEventListener('click', captureView);
fullscreenButton.addEventListener('click', toggleFullscreen);
saveButton.addEventListener('click', generateBlenderModel);

document.addEventListener('fullscreenchange', () => {
  const icon = fullscreenButton.querySelector('svg');
  fullscreenButton.setAttribute('aria-label', document.fullscreenElement ? '退出全屏' : '进入全屏');
  fullscreenButton.dataset.tooltip = document.fullscreenElement ? '退出全屏' : '全屏';
  if (icon && window.lucide) {
    const replacement = document.createElement('i');
    replacement.setAttribute('data-lucide', document.fullscreenElement ? 'minimize' : 'maximize');
    icon.replaceWith(replacement);
    window.lucide.createIcons({ nodes: [replacement] });
  }
});

window.addEventListener('resize', handleResize);

state.demoModel = createPoloModel();
state.currentModel = state.demoModel;
scene.add(state.demoModel);
handleResize();
setAutoRotate(controls.autoRotate);
if (previewConfig) {
  runPreviewMode(previewConfig);
} else {
  loadDefaultModel();
}

window.lucide?.createIcons({ attrs: { 'stroke-width': 1.9 } });

let lastFrame = performance.now();
function animate(now) {
  requestAnimationFrame(animate);
  const delta = Math.min((now - lastFrame) / 1000, 0.05);
  lastFrame = now;
  controls.update(delta);
  renderer.render(scene, camera);
}
requestAnimationFrame(animate);

window.__CLOTHING_VIEWER__ = {
  scene,
  camera,
  renderer,
  controls,
  state,
};
