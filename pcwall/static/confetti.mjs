// A single bounded animation loop, including when several kids tap together.
const running = new WeakMap();

export function burst(canvas, colors = ["#EAC586", "#10B981"], options = {}) {
  if (!canvas || (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches)) {
    return Promise.resolve();
  }
  const context = canvas.getContext("2d");
  if (!context) return Promise.resolve();
  const previous = running.get(canvas);
  if (previous) { cancelAnimationFrame(previous.frame); previous.resolve(); }
  const width = window.innerWidth;
  const height = window.innerHeight;
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  const palette = colors.length ? colors : ["#EAC586"];
  const count = Math.min(140, Math.max(1, options.count || 110));
  const duration = Math.min(3000, Math.max(200, options.duration || 2200));
  const originX = Number.isFinite(options.x) ? options.x : width / 2;
  const originY = Number.isFinite(options.y) ? options.y : height * .45;
  const pieces = Array.from({ length: count }, (_, index) => ({
    x: originX, y: originY, vx: (Math.random() - .5) * 650,
    vy: -180 - Math.random() * 430, angle: Math.random() * Math.PI,
    spin: (Math.random() - .5) * 8, size: 5 + Math.random() * 7,
    color: palette[index % palette.length],
  }));
  return new Promise((resolve) => {
    const job = { frame: 0, resolve };
    running.set(canvas, job);
    const start = performance.now();
    let last = start;
    function frame(now) {
      const elapsed = now - start;
      const delta = Math.min((now - last) / 1000, .04);
      last = now;
      context.clearRect(0, 0, width, height);
      if (elapsed >= duration || document.hidden) {
        context.globalAlpha = 1;
        running.delete(canvas);
        resolve();
        return;
      }
      context.globalAlpha = Math.min(1, (duration - elapsed) / 650);
      pieces.forEach((piece) => {
        piece.vy += 520 * delta;
        piece.x += piece.vx * delta;
        piece.y += piece.vy * delta;
        piece.angle += piece.spin * delta;
        context.save();
        context.translate(piece.x, piece.y);
        context.rotate(piece.angle);
        context.fillStyle = piece.color;
        context.fillRect(-piece.size / 2, -piece.size / 4, piece.size, piece.size / 2);
        context.restore();
      });
      job.frame = requestAnimationFrame(frame);
    }
    job.frame = requestAnimationFrame(frame);
  });
}
