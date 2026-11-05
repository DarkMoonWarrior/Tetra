// 背景：几个高中立体几何里常见的图形（正方体、正四面体、正八面体、正四棱锥、三棱柱），
// 用细线画在工作区四周，慢慢转动；被挡住的棱画成虚线，和课本插图的画法一样。
// 出题时转得快一些；系统设置了"减少动态效果"时只画一帧静止的图。

const SOLIDS = {
  cube: {
    vertices: [[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1], [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]]
      .map((v) => v.map((c) => c / Math.sqrt(3))),
    faces: [[0, 1, 2, 3], [4, 5, 6, 7], [0, 1, 5, 4], [2, 3, 7, 6], [1, 2, 6, 5], [0, 3, 7, 4]],
  },
  tetra: {
    vertices: [[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]].map((v) => v.map((c) => c / Math.sqrt(3))),
    faces: [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]],
  },
  octa: {
    vertices: [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]],
    faces: [[0, 2, 4], [0, 2, 5], [0, 3, 4], [0, 3, 5], [1, 2, 4], [1, 2, 5], [1, 3, 4], [1, 3, 5]],
  },
  pyramid: {
    vertices: [[-0.7, -0.5, -0.7], [0.7, -0.5, -0.7], [0.7, -0.5, 0.7], [-0.7, -0.5, 0.7], [0, 0.95, 0]],
    faces: [[0, 1, 2, 3], [0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]],
  },
  prism: {
    vertices: [[0, -0.6, 0.8], [-0.69, -0.6, -0.4], [0.69, -0.6, -0.4], [0, 0.6, 0.8], [-0.69, 0.6, -0.4], [0.69, 0.6, -0.4]],
    faces: [[0, 1, 2], [3, 4, 5], [0, 1, 4, 3], [1, 2, 5, 4], [2, 0, 3, 5]],
  },
};

// 每条棱属于哪两个面：两个面都背对我们，这条棱就被挡住了
for (const solid of Object.values(SOLIDS)) {
  const edges = new Map();
  solid.faces.forEach((face, f) => {
    face.forEach((a, i) => {
      const b = face[(i + 1) % face.length];
      const key = a < b ? `${a}-${b}` : `${b}-${a}`;
      if (!edges.has(key)) edges.set(key, { a: Math.min(a, b), b: Math.max(a, b), faces: [] });
      edges.get(key).faces.push(f);
    });
  });
  solid.edges = [...edges.values()];
}

// 位置按工作区的宽高取比例，尽量放在四周，少压住中间的文字
const LAYOUT = [
  { solid: 'cube', x: 0.12, y: 0.2, r: 0.075, spin: 0.11, tilt: -0.42, phase: 0.3 },
  { solid: 'tetra', x: 0.87, y: 0.16, r: 0.07, spin: -0.14, tilt: -0.3, phase: 1.2 },
  { solid: 'octa', x: 0.9, y: 0.63, r: 0.08, spin: 0.09, tilt: -0.5, phase: 2.1 },
  { solid: 'pyramid', x: 0.1, y: 0.74, r: 0.075, spin: -0.1, tilt: -0.38, phase: 0.8 },
  { solid: 'prism', x: 0.36, y: 0.08, r: 0.05, spin: 0.13, tilt: -0.45, phase: 1.7 },
  { solid: 'cube', x: 0.66, y: 0.93, r: 0.045, spin: -0.12, tilt: -0.35, phase: 2.7 },
];
// 窄屏上文字几乎占满宽度，只留右上和左下两个，并且画小一点
const NARROW = new Set([1, 3]);
const CAMERA = 4.2;

const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
let canvas = null;
let ctx = null;
let width = 0;
let height = 0;
let clock = 0;
let speed = 1;
let targetSpeed = 1;
let lastTime = performance.now();
let lastDraw = 0;

function sub(p, q) { return [p[0] - q[0], p[1] - q[1], p[2] - q[2]]; }
function dot(p, q) { return p[0] * q[0] + p[1] * q[1] + p[2] * q[2]; }
function cross(p, q) { return [p[1] * q[2] - p[2] * q[1], p[2] * q[0] - p[0] * q[2], p[0] * q[1] - p[1] * q[0]]; }
function mean(points) {
  const sum = [0, 0, 0];
  for (const p of points) { sum[0] += p[0]; sum[1] += p[1]; sum[2] += p[2]; }
  return sum.map((c) => c / points.length);
}

function drawSolid(item) {
  const solid = SOLIDS[item.solid];
  const unit = Math.min(width, height);
  const radius = width < 720 ? Math.max(26, item.r * unit * 0.8) : Math.max(34, item.r * unit);
  const cx = item.x * width;
  const cy = item.y * height + Math.sin(clock * 0.35 + item.phase) * radius * 0.08;
  const yaw = item.phase + clock * item.spin;
  const tilt = item.tilt + Math.sin(clock * 0.12 + item.phase) * 0.06;
  const [cosY, sinY, cosX, sinX] = [Math.cos(yaw), Math.sin(yaw), Math.cos(tilt), Math.sin(tilt)];

  // 先绕竖直轴转，再前后倾一点，像从斜上方看
  const points = solid.vertices.map(([x, y, z]) => {
    const x1 = x * cosY + z * sinY;
    const z1 = -x * sinY + z * cosY;
    return [x1, y * cosX - z1 * sinX, y * sinX + z1 * cosX];
  });
  const center = mean(points);
  const eye = [0, 0, -CAMERA];
  const facing = solid.faces.map((face) => {
    const corners = face.map((i) => points[i]);
    let normal = cross(sub(corners[1], corners[0]), sub(corners[2], corners[0]));
    const middle = mean(corners);
    if (dot(normal, sub(middle, center)) < 0) normal = normal.map((c) => -c);
    return dot(normal, sub(middle, eye)) < 0;
  });
  const screen = points.map(([x, y, z]) => {
    const k = CAMERA / (CAMERA + z);
    return [cx + x * k * radius, cy - y * k * radius];
  });

  const line = (edge) => {
    ctx.beginPath();
    ctx.moveTo(...screen[edge.a]);
    ctx.lineTo(...screen[edge.b]);
    ctx.stroke();
  };
  ctx.lineWidth = 1.1;
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = `rgba(${ink}, 0.13)`;
  for (const edge of solid.edges) if (!edge.faces.some((f) => facing[f])) line(edge);
  ctx.setLineDash([]);
  ctx.strokeStyle = `rgba(${ink}, 0.3)`;
  for (const edge of solid.edges) if (edge.faces.some((f) => facing[f])) line(edge);

  ctx.fillStyle = `rgba(${ink}, 0.32)`;
  const seen = new Set();
  solid.faces.forEach((face, f) => { if (facing[f]) face.forEach((i) => seen.add(i)); });
  for (const i of seen) {
    ctx.beginPath();
    ctx.arc(screen[i][0], screen[i][1], 1.7, 0, Math.PI * 2);
    ctx.fill();
  }
}

// 线条的颜色跟着主题走：白天是黑线，夜晚是白线（app.css 里的 --geo-rgb）
let ink = '0, 0, 0';

function draw() {
  ink = getComputedStyle(document.documentElement).getPropertyValue('--geo-rgb').trim() || '0, 0, 0';
  ctx.clearRect(0, 0, width, height);
  ctx.lineCap = 'round';
  LAYOUT.forEach((item, i) => {
    if (width < 720 && !NARROW.has(i)) return;
    drawSolid(item);
  });
}

function resize() {
  const box = canvas.parentElement.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  width = box.width;
  height = box.height;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

function frame(now) {
  const dt = Math.min(0.1, (now - lastTime) / 1000);
  lastTime = now;
  speed += (targetSpeed - speed) * Math.min(1, dt * 1.5);
  clock += dt * speed;
  // 背景不需要 60 帧，约 30 帧就够，省电
  if (now - lastDraw > 32) {
    draw();
    lastDraw = now;
  }
  requestAnimationFrame(frame);
}

export function startGeometry(node) {
  canvas = node;
  ctx = canvas.getContext('2d');
  new ResizeObserver(resize).observe(canvas.parentElement);
  resize();
  if (!reduceMotion.matches) requestAnimationFrame(frame);
}

export function setGeometryBusy(busy) {
  targetSpeed = busy ? 2.4 : 1;
}
