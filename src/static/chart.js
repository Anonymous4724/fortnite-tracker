/* A small SVG chart engine, no external dependency, works offline.
   drawChart(el, config) — see app.js for the call sites. */
(function (global) {
  const NS = 'http://www.w3.org/2000/svg';

  const PALETTE = ['#4da3ff', '#2ecc8f', '#ffb340', '#ff5c72', '#a77bff', '#37d3d3',
                   '#ff8fb1', '#c3d94e'];

  function el(name, attrs, parent) {
    const node = document.createElementNS(NS, name);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }

  function niceTicks(min, max, count) {
    if (!isFinite(min) || !isFinite(max)) return [0, 1];
    if (max - min < 1e-9) { max = min + 1; }
    const raw = (max - min) / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    const start = Math.ceil(min / step) * step;
    const ticks = [];
    for (let v = start; v <= max + step * 0.001; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    return ticks;
  }

  function drawChart(container, cfg) {
    container.innerHTML = '';
    const series = (cfg.series || []).filter(s => s.points && s.points.length);
    const W = cfg.width || container.clientWidth || 820;
    const H = cfg.height || 380;
    const M = { t: 16, r: 18, b: 42, l: 58 };

    const wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    container.appendChild(wrap);

    const svg = el('svg', {
      class: 'chart', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none',
      style: `height:${H}px`
    }, wrap);

    if (!series.length) {
      el('text', { x: W / 2, y: H / 2, fill: '#8b95a8', 'text-anchor': 'middle',
                   'font-size': 13 }, svg).textContent =
        cfg.emptyText || (typeof T === 'function' ? T('Nothing to plot yet.')
                                          : 'Nothing to plot yet.');
      return;
    }

    let xs = [], ys = [];
    series.forEach(s => s.points.forEach(p => { xs.push(p.x); ys.push(p.y); }));
    series.forEach(s => (s.band || []).forEach(b => { xs.push(b.x); ys.push(b.lo); ys.push(b.hi); }));

    let xMin = cfg.xMin !== undefined ? cfg.xMin : Math.min(...xs);
    let xMax = cfg.xMax !== undefined ? cfg.xMax : Math.max(...xs);
    let yMin = cfg.yMin !== undefined ? cfg.yMin : Math.min(...ys);
    let yMax = cfg.yMax !== undefined ? cfg.yMax : Math.max(...ys);
    if (xMax - xMin < 1e-9) xMax = xMin + 1;
    const pad = (yMax - yMin) * 0.12 || Math.abs(yMax) * 0.1 || 1;
    yMin = cfg.yMin !== undefined ? cfg.yMin : Math.max(0, yMin - pad);
    yMax = yMax + pad;

    const X = v => M.l + (v - xMin) / (xMax - xMin) * (W - M.l - M.r);
    const Y = v => H - M.b - (v - yMin) / (yMax - yMin) * (H - M.t - M.b);

    const fmtX = cfg.xFormat || (v => String(Math.round(v)));
    const fmtY = cfg.yFormat || (v => (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString('fr-FR')
                                                           : String(Math.round(v * 10) / 10)));

    /* grille + axes */
    niceTicks(yMin, yMax, 5).forEach(t => {
      const y = Y(t);
      if (y < M.t - 1 || y > H - M.b + 1) return;
      el('line', { x1: M.l, x2: W - M.r, y1: y, y2: y, stroke: '#242c3d', 'stroke-width': 1 }, svg);
      el('text', { x: M.l - 9, y: y + 4, fill: '#8b95a8', 'font-size': 11,
                   'text-anchor': 'end', 'font-family': 'ui-monospace, monospace' }, svg)
        .textContent = fmtY(t);
    });
    (cfg.xTicks || niceTicks(xMin, xMax, 6)).forEach(t => {
      const x = X(t);
      if (x < M.l - 1 || x > W - M.r + 1) return;
      el('line', { x1: x, x2: x, y1: M.t, y2: H - M.b, stroke: '#1c2333', 'stroke-width': 1 }, svg);
      el('text', { x: x, y: H - M.b + 18, fill: '#8b95a8', 'font-size': 11,
                   'text-anchor': 'middle' }, svg).textContent = fmtX(t);
    });
    el('line', { x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, stroke: '#3a4459' }, svg);
    el('line', { x1: M.l, x2: M.l, y1: M.t, y2: H - M.b, stroke: '#3a4459' }, svg);

    /* marqueurs verticaux (ex : "maintenant") */
    (cfg.marks || []).forEach(m => {
      const x = X(m.x);
      el('line', { x1: x, x2: x, y1: M.t, y2: H - M.b, stroke: m.color || '#ffb340',
                   'stroke-width': 1, 'stroke-dasharray': '4 4', opacity: .8 }, svg);
      if (m.label) {
        el('text', { x: x + 5, y: M.t + 12, fill: m.color || '#ffb340', 'font-size': 11 }, svg)
          .textContent = m.label;
      }
    });

    const layers = el('g', {}, svg);

    series.forEach((s, i) => {
      const color = s.color || PALETTE[i % PALETTE.length];
      s._color = color;
      const g = el('g', { 'data-series': i }, layers);
      s._g = g;
      if (s.hidden) g.setAttribute('opacity', 0);

      if (s.band && s.band.length > 1) {
        const up = s.band.map(b => `${X(b.x)},${Y(b.hi)}`).join(' ');
        const down = s.band.slice().reverse().map(b => `${X(b.x)},${Y(b.lo)}`).join(' ');
        el('polygon', { points: up + ' ' + down, fill: color, opacity: .12 }, g);
      }

      const pts = s.points.map(p => `${X(p.x)},${Y(p.y)}`).join(' ');
      el('polyline', {
        points: pts, fill: 'none', stroke: color, 'stroke-width': s.width || 2.2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round',
        'stroke-dasharray': s.dashed ? '6 5' : '', opacity: s.dashed ? .85 : 1
      }, g);

      if (s.showDots !== false) {
        s.points.forEach(p => {
          el('circle', { cx: X(p.x), cy: Y(p.y), r: s.dashed ? 0 : 3.4, fill: '#0e1117',
                         stroke: color, 'stroke-width': 2 }, g);
        });
      }
    });

    /* survol : ligne verticale + infobulle */
    const hover = el('line', { y1: M.t, y2: H - M.b, stroke: '#63708c', opacity: 0 }, svg);
    const tip = document.createElement('div');
    tip.className = 'tooltip';
    tip.style.display = 'none';
    wrap.appendChild(tip);

    svg.addEventListener('mousemove', ev => {
      const rect = svg.getBoundingClientRect();
      const scale = W / rect.width;
      const px = (ev.clientX - rect.left) * scale;
      if (px < M.l || px > W - M.r) { tip.style.display = 'none'; hover.setAttribute('opacity', 0); return; }
      const xVal = xMin + (px - M.l) / (W - M.l - M.r) * (xMax - xMin);

      let rows = [], bestX = null, bestDist = Infinity;
      series.forEach(s => {
        if (s.hidden || s.noTooltip) return;
        let best = null, dist = Infinity;
        s.points.forEach(p => {
          const d = Math.abs(p.x - xVal);
          if (d < dist) { dist = d; best = p; }
        });
        if (best && dist < (xMax - xMin) * 0.08) {
          rows.push(`<span style="color:${s._color}">&#9632;</span> ${s.name} : <b>${fmtY(best.y)}</b>`);
          if (dist < bestDist) { bestDist = dist; bestX = best.x; }
        }
      });
      if (!rows.length) { tip.style.display = 'none'; hover.setAttribute('opacity', 0); return; }

      hover.setAttribute('x1', X(bestX));
      hover.setAttribute('x2', X(bestX));
      hover.setAttribute('opacity', .5);
      tip.innerHTML = `<div style="color:#8b95a8;margin-bottom:4px">${fmtX(bestX)}</div>` + rows.join('<br>');
      tip.style.display = 'block';
      const box = svg.getBoundingClientRect();
      tip.style.left = (X(bestX) / W * box.width) + 'px';
      tip.style.top = '10px';
    });
    svg.addEventListener('mouseleave', () => {
      tip.style.display = 'none';
      hover.setAttribute('opacity', 0);
    });

    /* legende cliquable */
    if (cfg.legend !== false) {
      const legend = document.createElement('div');
      legend.className = 'legend';
      const seen = new Set();
      series.forEach((s, i) => {
        if (s.noLegend || seen.has(s.name)) return;
        seen.add(s.name);
        const item = document.createElement('span');
        item.className = 'item' + (s.hidden ? ' off' : '');
        item.innerHTML = `<span class="swatch" style="background:${s._color}"></span>${s.name}`;
        item.onclick = () => {
          const off = !item.classList.contains('off');
          item.classList.toggle('off', off);
          series.forEach(t => {
            if (t.name === s.name && t._g) {
              t.hidden = off;
              t._g.setAttribute('opacity', off ? 0 : 1);
            }
          });
        };
        legend.appendChild(item);
      });
      container.appendChild(legend);
    }
  }

  global.drawChart = drawChart;
  global.CHART_PALETTE = PALETTE;
})(window);
