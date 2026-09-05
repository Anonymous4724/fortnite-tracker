/* Helpers shared by every page. */

function api(url, method = 'GET', payload) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (payload !== undefined) opts.body = JSON.stringify(payload);
  return fetch(url, opts).then(r => r.json().catch(() => ({ ok: r.ok })));
}

let _toastTimer = null;
function toast(msg, isError) {
  document.querySelectorAll('.toast').forEach(t => t.remove());
  const div = document.createElement('div');
  div.className = 'toast' + (isError ? ' err' : '');
  div.textContent = msg;
  document.body.appendChild(div);
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => div.remove(), 2800);
}

/* Thousands separators and the 24-hour clock follow the language the reader
 * picked, not the browser's, so a page never mixes the two. */
const LOCALE = (document.documentElement.lang || 'en') === 'fr' ? 'fr-FR' : 'en-GB';

function fmtNum(v) {
  if (v === null || v === undefined) return '—';
  return Math.round(v).toLocaleString(LOCALE);
}

function fmtClock(minutes, startISO) {
  const d = new Date(startISO.replace(' ', 'T'));
  d.setMinutes(d.getMinutes() + minutes);
  return d.toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' });
}

function localNow() {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
}

/* ---------- scoring tables: text <-> structure ---------- */
function scoringToText(s) {
  return ((s && s.placement) || []).map(row =>
    (row[0] === row[1] ? row[0] : row[0] + '-' + row[1]) + ' = ' + row[2]).join('\n');
}

/* Parse a scoring table pasted from wherever the reader found it.
 *
 * Accepted formats, mixable inside one block:
 *    1 = 65            2-3 = 55         (what this app writes)
 *    1st  65 (+9)  32                   (leaderboard sites, several columns)
 *    1er  65          Top 1 : 65        (French rankings, prefixes)
 *    #1   65
 * Anything in parentheses is a gap to the rank above and gets dropped.
 * When several point columns follow the rank they are all kept, and the page
 * asks which one to use — rules PDFs often print Solo/Duo/Squad side by side.
 */
function parsePlacementTable(text) {
  const rows = [];
  let columns = 0;
  let kill = null;

  (text || '').split(/\r?\n/).forEach(line => {
    if (!line.trim()) return;

    // A line like "eliminations: 2" sets the value of a kill.
    const killMatch = line.match(/(?:elim|kill|élim)\w*[^\d\-+]*([+-]?\d+(?:[.,]\d+)?)/i);
    if (killMatch && !/^\s*(?:top\s*)?#?\d/.test(line)) {
      kill = parseFloat(killMatch[1].replace(',', '.'));
      return;
    }

    const m = line.match(
      /^\s*(?:top\s*|place\s*|#)?(\d+)\s*(?:st|nd|rd|th|ers?|ères?|èmes?|emes?|e)?\s*(?:[-–—]\s*(?:top\s*|#)?(\d+)\s*(?:st|nd|rd|th|ers?|èmes?|emes?|e)?)?\s*[=:\t ]\s*(.+)$/i);
    if (!m) return;

    const low = parseInt(m[1], 10);
    const high = m[2] ? parseInt(m[2], 10) : low;
    const rest = m[3].replace(/\([^)]*\)/g, ' ');          // "(+9)" is a gap, not points
    const values = (rest.match(/-?\d+(?:[.,]\d+)?/g) || [])
      .map(v => parseFloat(v.replace(',', '.')));
    if (!values.length) return;

    columns = Math.max(columns, values.length);
    rows.push({ low, high: Math.max(high, low), values });
  });

  // Some sites print a second column giving each placement in "eliminations
  // equivalent". The ratio between the two columns is then the kill value, and
  // it is the same on every line — which is how we recognise the case.
  let killFromColumns = null;
  if (columns >= 2) {
    const ratios = rows
      .filter(r => r.values.length >= 2 && r.values[1] > 0 && r.values[0] > 0)
      .map(r => r.values[0] / r.values[1]);
    if (ratios.length >= 4) {
      const sorted = ratios.slice().sort((a, b) => a - b);
      const median = sorted[Math.floor(sorted.length / 2)];
      const spread = Math.max(...ratios) - Math.min(...ratios);
      // Those sites round down, so allow a little spread before believing it.
      if (median >= 1.2 && spread <= median * 0.25) {
        killFromColumns = Math.round(median * 2) / 2;
      }
    }
  }

  return { rows, columns, kill, killFromColumns };
}

/* Column `col` of a parsed table -> tiers [[from, to, points], ...] */
function placementFromTable(parsed, col = 0) {
  const out = [];
  (parsed.rows || []).forEach(r => {
    const value = r.values[Math.min(col, r.values.length - 1)];
    if (value === undefined || isNaN(value)) return;
    const last = out[out.length - 1];
    if (last && last[2] === value && r.low === last[1] + 1) last[1] = r.high;
    else out.push([r.low, r.high, value]);
  });
  return out.filter(row => row[2] > 0);
}

function textToPlacement(text, col = 0) {
  return placementFromTable(parsePlacementTable(text), col);
}

function textToScoring(text, kill, col = 0) {
  const parsed = parsePlacementTable(text);
  const given = (kill !== undefined && kill !== null && String(kill).trim() !== '')
    ? parseFloat(kill) : null;
  const killValue = given !== null ? given : (parsed.kill ?? parsed.killFromColumns);
  return { kill: killValue || 0, placement: placementFromTable(parsed, col) };
}

/* Paste helper for a scoring field. Says how many tiers were recognised and,
 * when the pasted table has several columns, lets you pick one before tidying
 * the field up. Scoring tables get copied out of Epic's rules PDF, which is
 * why this exists at all. */
function attachScoringPaste(textareaId, hintId, killId) {
  const area = document.getElementById(textareaId);
  const hint = document.getElementById(hintId);
  if (!area || !hint) return;
  let column = 0;

  function refresh() {
    const parsed = parsePlacementTable(area.value);
    if (!parsed.rows.length) {
      hint.innerHTML = area.value.trim()
        ? `<span style="color:var(--warn)">${T('No tier recognised — one line per rank.')}</span>`
        : '';
      return;
    }
    if (parsed.kill !== null && killId) {
      const killField = document.getElementById(killId);
      if (killField && !killField.dataset.touched) killField.value = parsed.kill;
    }
    const table = placementFromTable(parsed, column);
    let html = `<b>${parsed.rows.length}</b> ${T('ranks recognised')}, ${table.length} ${T('tier(s)')}`;

    if (parsed.killFromColumns) {
      // A second column of elimination-equivalents gives away the kill value.
      const killField = killId && document.getElementById(killId);
      if (killField && !killField.dataset.touched) killField.value = parsed.killFromColumns;
      html += `<div style="margin-top:6px;color:var(--ok)">
        ${T('2nd column read as the elimination equivalent')}
        → <b>${parsed.killFromColumns} ${T('pt(s) per elimination')}</b>, ${T('column 1 used')}.</div>
        <div class="small muted">${T('So a top 1 worth')} ${parsed.rows[0].values[0]}
        ${T('pts equals')} ${parsed.rows[0].values[1]} ${T('eliminations')}.</div>`;
    } else if (parsed.columns > 1) {
      const choices = [];
      for (let c = 0; c < parsed.columns; c++) {
        const preview = parsed.rows.slice(0, 3)
          .map(r => r.values[Math.min(c, r.values.length - 1)]).join(', ');
        choices.push(`<button type="button" class="small ${c === column ? '' : 'ghost'}"
          onclick="window.__pickColumn('${textareaId}', ${c})">${T('column')} ${c + 1} (${preview}…)</button>`);
      }
      html += `<div class="row tight" style="margin-top:6px">
        <span class="small muted" style="align-self:center">${parsed.columns} ${T('columns detected')}:</span>
        ${choices.join('')}</div>`;
    }
    hint.innerHTML = html;
  }

  window.__pickColumn = (id, c) => {
    if (id !== textareaId) return;
    column = c;
    const parsed = parsePlacementTable(area.value);
    area.value = placementFromTable(parsed, c)
      .map(([a, b, p]) => (a === b ? a : a + '-' + b) + ' = ' + p).join('\n');
    column = 0;
    refresh();
  };

  area.addEventListener('input', refresh);
  area.addEventListener('paste', () => setTimeout(refresh, 20));
  if (killId) {
    const killField = document.getElementById(killId);
    if (killField) killField.addEventListener('input', () => { killField.dataset.touched = '1'; });
  }
  refresh();
}
