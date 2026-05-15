// TABLE — render a {_type: "TABLE", columns: [...], rows: [[...]]}
// data part as a real HTML <table>. The Python builder side lives in
// fast_a2a_app/server/artifacts/TABLE.py; the two meet at the "_type"
// discriminator only.
//
// Each renderer in this directory self-registers in window.A2A_RENDERERS
// when its script runs. build_a2a_ui concatenates every *.js file in
// this directory into the served HTML at boot, so dropping a new
// CAPS_NAME.js file is enough — no edit to index.html or to any
// Python file is required.
//
// Globals it relies on (defined in the bundled UI's index.html):
//   * el(tag, classes) — shared DOM-creation helper.
//
// Tailwind classes mirror the bundled chat UI's typography. The
// .table-zebra class drives row striping + borders via plain CSS in
// the <style> block of index.html — Tailwind opacity utilities like
// bg-slate-50/50 don't have dark-mode overrides and would wash out
// white text on dark.

window.A2A_RENDERERS = window.A2A_RENDERERS || {};

window.A2A_RENDERERS["TABLE"] = (value) => {
    const columns = value.columns || [];
    const rows = value.rows || [];

    const wrap = el('div',
        'mt-2 rounded-lg border border-slate-200 overflow-x-auto bg-white');
    const table = el('table',
        'min-w-full text-xs border-collapse table-zebra');

    if (columns.length > 0) {
        const thead = el('thead', 'bg-slate-50');
        const tr = el('tr', '');
        for (const col of columns) {
            const th = el('th',
                'px-3 py-2 text-left font-semibold text-slate-700 whitespace-nowrap');
            th.textContent = String(col);
            tr.appendChild(th);
        }
        thead.appendChild(tr);
        table.appendChild(thead);
    }

    // Cell formatter — nulls render as a faint em-dash so the eye can
    // scan for missing values; numbers / strings are coerced; anything
    // else gets a compact JSON dump as a last resort.
    const fmt = (v) => {
        if (v === null || v === undefined) return '—';
        if (typeof v === 'object') return JSON.stringify(v);
        return String(v);
    };

    const tbody = el('tbody', '');
    for (const row of rows) {
        const tr = el('tr', '');
        const cells = Array.isArray(row) ? row : [row];
        for (const cell of cells) {
            const isNum = typeof cell === 'number';
            const td = el('td',
                'px-3 py-1.5 align-top whitespace-nowrap ' +
                (isNum ? 'text-right tabular-nums font-mono text-slate-800' : 'text-slate-700'));
            td.textContent = fmt(cell);
            tr.appendChild(td);
        }
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
};
