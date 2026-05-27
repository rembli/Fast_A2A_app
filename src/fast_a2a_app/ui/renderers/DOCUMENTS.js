// DOCUMENTS — always-on workspace file panel.
//
// Payload shape (built server-side by ``documents_artifact`` in
// office-agent/src/artifacts.py):
//   {
//       _type: "DOCUMENTS",
//       documents: [
//           {
//               filename:   "quarterly-update.pptx",
//               downloadUrl: "/download/<ctx>/quarterly-update.pptx",
//               mediaType:  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
//               sizeBytes:  31415,
//               modifiedAt: "2026-05-22T23:04:50Z",   // optional
//               viewable:   true,                       // optional, default true
//               versions:   [                            // optional, newest-first
//                   {
//                       filename: "quarterly-update.20260522T230450123456.pptx",
//                       downloadUrl: "/download/<ctx>/.versions/quarterly-update.20260522T230450123456.pptx",
//                       sizeBytes: 30912, modifiedAt: "2026-05-22T23:04:50Z",
//                       viewable: true,
//                   },
//                   ...
//               ],
//           },
//           ...
//       ],
//   }
//
// Instead of inserting a card into the chat scroll, this renderer
// maintains a fixed-position side panel docked to the right edge of
// the chat. The panel is always visible (collapsible via a small lug
// on its left edge) and silently updates every time the agent emits a
// DOCUMENTS artifact — which it does at the end of every successful
// turn, so the panel reflects current workspace state without the user
// having to ask. Clicking a row fires ``/view <filename>`` via
// ``sendSuggestion`` exactly as the previous inline list did.
//
// Per-row version history: every row with ``versions.length > 0`` gets
// a small chevron that toggles an indented sub-list of prior versions.
// Clicking a version row fires ``/view .versions/<versioned-filename>``
// — the agent's slash handler accepts that one-segment ``.versions/``
// prefix and renders the historical file in a DOCUMENT card.

(function () {
    'use strict';

    window.A2A_RENDERERS = window.A2A_RENDERERS || {};

    const PANEL_ID = 'office-docs-side-panel';
    const COLLAPSED_KEY = 'office-agent-docs-panel-collapsed';
    const MARKER_ATTR = 'data-documents-hidden-marker';

    // Per-row expand state for the version history sub-list, keyed by
    // the latest-version filename. Kept in memory only — version
    // history is a transient inspection affordance, not worth burning
    // a localStorage slot on.
    const expandedRows = new Set();

    // ── Persisted collapse state ─────────────────────────────────────
    const getCollapsed = () => {
        try { return localStorage.getItem(COLLAPSED_KEY) === '1'; }
        catch (_) { return false; }
    };
    const setCollapsed = (v) => {
        try { localStorage.setItem(COLLAPSED_KEY, v ? '1' : '0'); }
        catch (_) { /* non-persistent collapse is fine */ }
    };

    // ── Tiny DOM helper (mirrors app.js's ``el``) ────────────────────
    // We can't rely on the global ``el`` from ``app.js`` here because
    // this renderer JS is inlined ABOVE the ``<script src="app.js" defer>``
    // tag in ``index.html`` — at parse time ``el`` is not yet defined,
    // and ``restoreTranscript()`` (which app.js runs synchronously
    // before DOMContentLoaded fires) may invoke this renderer the
    // instant app.js starts. A local copy sidesteps the ordering
    // problem entirely.
    const mkEl = (tag, cls) => {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        return node;
    };

    // ── Panel singleton ──────────────────────────────────────────────
    let panelEl = null;
    let lugEl = null;
    let listEl = null;
    let countEl = null;
    let arrowEl = null;

    const applyCollapsedState = () => {
        if (!panelEl || !lugEl || !arrowEl) return;
        const collapsed = getCollapsed();
        if (collapsed) {
            // Slide the panel off-screen. The lug stays visible because
            // it lives at ``-left-9`` outside the panel's box; once the
            // panel is fully translated right the lug ends up just
            // inside the viewport's right edge.
            panelEl.style.transform = 'translateX(100%)';
            arrowEl.textContent = '❮';
            lugEl.setAttribute('aria-expanded', 'false');
            lugEl.title = 'Show workspace panel';
        } else {
            panelEl.style.transform = 'translateX(0)';
            arrowEl.textContent = '❯';
            lugEl.setAttribute('aria-expanded', 'true');
            lugEl.title = 'Hide workspace panel';
        }
    };

    const ensurePanel = () => {
        if (panelEl) return panelEl;
        const existing = document.getElementById(PANEL_ID);
        if (existing) { panelEl = existing; return panelEl; }
        if (!document.body) return null;

        const panel = mkEl(
            'aside',
            'fixed right-0 z-30 bg-white ' +
                'border-l border-slate-200 shadow-lg ' +
                'transition-transform duration-200 ease-in-out'
        );
        panel.id = PANEL_ID;
        panel.style.width = '300px';
        // Sensible initial top/bottom; ``alignToChat`` overrides as soon
        // as it can measure ``#messages`` + ``#composer``.
        panel.style.top = '0px';
        panel.style.bottom = '0px';

        // Collapse lug — small tab-shaped protrusion on the LEFT edge,
        // positioned outside the panel's box so the lug stays visible
        // even when the panel slides off-screen on collapse.
        const lug = mkEl(
            'button',
            'absolute top-1/2 -translate-y-1/2 -left-9 w-9 h-20 ' +
                'flex items-center justify-center bg-white ' +
                'border border-r-0 border-slate-200 rounded-l-lg ' +
                'shadow-md hover:bg-slate-50 ' +
                'text-slate-500 hover:text-slate-700 transition'
        );
        lug.type = 'button';
        lug.setAttribute('aria-label', 'Toggle workspace panel');
        const arrow = mkEl('span', 'text-base font-bold');
        lug.appendChild(arrow);

        const body = mkEl('div', 'h-full flex flex-col min-w-0');
        const header = mkEl(
            'div',
            'px-3 py-2 border-b border-slate-200 bg-slate-50 ' +
                'flex items-center justify-between gap-2'
        );
        const headerLeft = mkEl(
            'div', 'font-medium text-sm text-slate-700 truncate'
        );
        headerLeft.textContent = '📂 Workspace';
        const headerRight = mkEl(
            'span', 'text-[11px] text-slate-500 tabular-nums shrink-0'
        );
        headerRight.textContent = '—';
        header.appendChild(headerLeft);
        header.appendChild(headerRight);

        const list = mkEl('div', 'flex-1 overflow-y-auto min-h-0');

        body.appendChild(header);
        body.appendChild(list);
        panel.appendChild(body);
        panel.appendChild(lug);
        document.body.appendChild(panel);

        lug.addEventListener('click', () => {
            setCollapsed(!getCollapsed());
            applyCollapsedState();
        });

        panelEl = panel;
        lugEl = lug;
        listEl = list;
        countEl = headerRight;
        arrowEl = arrow;

        applyCollapsedState();
        renderEmpty();
        alignToChat();
        return panel;
    };

    // ── Layout alignment ─────────────────────────────────────────────
    // Anchor the panel's vertical extent to the chat layout. We prefer
    // to size against the composer (anchor the BOTTOM above it) and
    // the header chrome (anchor the TOP below it) because those are
    // the load-bearing pieces — ``#messages`` itself can briefly have
    // zero height during reflow, which used to make the panel cover
    // the composer.
    let resizeObserver = null;
    const alignToChat = () => {
        if (!panelEl) return;
        const composer = document.getElementById('composer');
        const messages = document.getElementById('messages');
        const headerEl = document.querySelector('header');
        const tabsBar = document.querySelector('.view-tabs-bar');
        const innerH = window.innerHeight;

        // Top — below the topmost shrink-0 chrome we can find. Falls
        // back to the messages container's top, then to 0.
        let top = 0;
        if (headerEl) {
            const r = headerEl.getBoundingClientRect();
            top = Math.max(top, r.bottom);
        }
        if (messages) {
            const r = messages.getBoundingClientRect();
            // Only adopt messages.top if it's a sane (positive) value;
            // a 0-height container can leave it at NaN-equivalent.
            if (r.top > top) top = r.top;
        }

        // Bottom — distance from viewport bottom up to the top of the
        // composer (preferred) or the tabs bar (fallback). Clamped so
        // the panel can never cover the composer even if measurements
        // are briefly off.
        let bottomGap = 0;
        if (composer) {
            const r = composer.getBoundingClientRect();
            bottomGap = Math.max(bottomGap, innerH - r.top);
        } else if (tabsBar) {
            const r = tabsBar.getBoundingClientRect();
            bottomGap = Math.max(bottomGap, innerH - r.top);
        } else if (messages) {
            const r = messages.getBoundingClientRect();
            bottomGap = Math.max(bottomGap, innerH - r.bottom);
        }

        // Final clamp — never let top + bottomGap exceed viewport
        // height (which would invert the panel) and never let either
        // value go negative.
        top = Math.max(0, Math.min(top, innerH - 40));
        bottomGap = Math.max(0, Math.min(bottomGap, innerH - top - 40));

        panelEl.style.top = `${top}px`;
        panelEl.style.bottom = `${bottomGap}px`;
    };

    const wireAlignment = () => {
        if (resizeObserver) return;
        window.addEventListener('resize', alignToChat);
        try {
            resizeObserver = new ResizeObserver(alignToChat);
            const targets = [
                document.body,
                document.getElementById('messages'),
                document.getElementById('composer'),
                document.querySelector('.view-tabs-bar'),
                document.querySelector('header'),
                document.getElementById('card-panel'),
                document.getElementById('context-bar'),
            ].filter(Boolean);
            for (const t of targets) resizeObserver.observe(t);
        } catch (_) {
            // ResizeObserver missing — window resize still keeps it OK.
        }
    };

    // ── Format helpers ───────────────────────────────────────────────
    const formatBytes = (n) => {
        if (typeof n !== 'number' || !isFinite(n) || n < 0) return '';
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    };

    const formatMtime = (iso) => {
        if (!iso || typeof iso !== 'string') return '';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        const now = new Date();
        const sameDay =
            d.getFullYear() === now.getFullYear() &&
            d.getMonth() === now.getMonth() &&
            d.getDate() === now.getDate();
        return sameDay
            ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    };

    const iconFor = (filename) => {
        const ext = (filename.split('.').pop() || '').toLowerCase();
        switch (ext) {
            case 'pptx': return '🎞️';
            case 'docx': return '📄';
            case 'xlsx': return '📊';
            case 'pdf':  return '📕';
            case 'csv':  return '🧮';
            case 'txt':  return '📝';
            default:     return '📁';
        }
    };

    // ── Row + list rendering ─────────────────────────────────────────

    function renderEmpty() {
        if (!listEl) return;
        listEl.innerHTML = '';
        const empty = mkEl(
            'div',
            'px-4 py-6 text-xs text-slate-500 text-center whitespace-pre-line'
        );
        empty.textContent =
            'Workspace is empty.\nAttach a file or ask me to build one.';
        listEl.appendChild(empty);
    }

    // Build one row. ``opts.viewPath`` is the value passed to
    // ``/view`` (defaults to ``filename``); used so version rows can
    // route through ``/view .versions/<filename>`` without burying the
    // prefix inside the filename column.
    function buildRow(doc, opts) {
        const filename = (doc && doc.filename) || '';
        if (!filename) return null;
        const viewable = doc.viewable !== false;
        const indented = !!(opts && opts.indented);
        const viewPath = (opts && opts.viewPath) || filename;
        const labelOverride = opts && opts.label;

        const row = mkEl(
            'div',
            'flex items-center gap-2 px-3 py-2 border-b border-slate-100 ' +
                'hover:bg-slate-50 transition ' +
                (indented ? 'pl-9 bg-slate-50/50' : '')
        );

        const main = mkEl(
            viewable ? 'button' : 'div',
            'flex items-center gap-2 flex-1 min-w-0 text-left ' +
                (viewable
                    ? 'cursor-pointer hover:text-blue-700'
                    : 'cursor-default text-slate-700')
        );
        if (viewable) {
            main.type = 'button';
            main.setAttribute('aria-label', `View ${labelOverride || filename}`);
        }

        const icon = mkEl(
            'span',
            'shrink-0 ' + (indented ? 'text-sm opacity-60' : 'text-lg')
        );
        icon.textContent = iconFor(filename);
        main.appendChild(icon);

        const nameCol = mkEl('div', 'min-w-0 flex flex-col');
        const nameLine = mkEl(
            'span',
            (indented ? 'text-xs' : 'text-sm font-medium') +
                ' truncate ' +
                (viewable ? 'text-slate-900' : 'text-slate-700')
        );
        nameLine.textContent = labelOverride || filename;
        nameCol.appendChild(nameLine);

        const metaBits = [];
        const sizeStr = formatBytes(doc.sizeBytes);
        if (sizeStr) metaBits.push(sizeStr);
        const mtimeStr = formatMtime(doc.modifiedAt);
        if (mtimeStr) metaBits.push(mtimeStr);
        if (!viewable) metaBits.push('download only');
        if (metaBits.length) {
            const meta = mkEl(
                'span', 'text-[11px] text-slate-500 truncate'
            );
            meta.textContent = metaBits.join(' · ');
            nameCol.appendChild(meta);
        }
        main.appendChild(nameCol);

        if (viewable) {
            main.addEventListener('click', () => {
                if (typeof window.sendSuggestion === 'function') {
                    window.sendSuggestion(`/view ${viewPath}`);
                }
            });
        }
        row.appendChild(main);

        const downloadLink = mkEl(
            'a',
            'shrink-0 inline-flex items-center justify-center w-7 h-7 ' +
                'rounded-md bg-slate-100 hover:bg-slate-200 ' +
                'text-slate-700 text-xs font-medium'
        );
        downloadLink.textContent = '↓';
        downloadLink.title = `Download ${filename}`;
        downloadLink.setAttribute('aria-label', `Download ${filename}`);
        downloadLink.href = doc.downloadUrl || '#';
        downloadLink.setAttribute('download', filename);
        row.appendChild(downloadLink);

        return row;
    }

    // Wrap a latest-version row with an optional expand toggle and an
    // indented sub-list of prior versions. Each prior version routes
    // ``/view`` through ``.versions/<filename>`` so the slash handler
    // surfaces THAT specific version in a DOCUMENT card.
    function buildGroupRows(doc) {
        const filename = (doc && doc.filename) || '';
        if (!filename) return [];
        const versions = Array.isArray(doc.versions) ? doc.versions : [];

        const latestRow = buildRow(doc, { indented: false });
        if (!latestRow) return [];

        if (versions.length === 0) {
            return [latestRow];
        }

        const isOpen = expandedRows.has(filename);
        const total = versions.length + 1;
        const label = `v${total}`;

        // Clearly-styled pill sitting between the filename column and
        // the download chip. Reads as a button at a glance: blue fill
        // when collapsed (call to action: "history available"),
        // outlined when expanded (state: "history shown"). The flip
        // arrow + version count remove ambiguity that the small
        // leading chevron used to leave.
        const toggle = mkEl(
            'button',
            'shrink-0 inline-flex items-center gap-1 px-2 h-7 ' +
                'rounded-md text-[11px] font-semibold tabular-nums ' +
                'border transition ' +
                (isOpen
                    ? 'bg-white text-blue-700 border-blue-300 hover:bg-blue-50'
                    : 'bg-blue-50 text-blue-700 border-blue-200 ' +
                      'hover:bg-blue-100')
        );
        toggle.type = 'button';
        toggle.title = isOpen
            ? `Hide ${versions.length} older version` +
              (versions.length === 1 ? '' : 's')
            : `Show ${versions.length} older version` +
              (versions.length === 1 ? '' : 's');
        toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        toggle.setAttribute('aria-label', toggle.title);

        const toggleLabel = mkEl('span', '');
        toggleLabel.textContent = label;
        const toggleArrow = mkEl('span', 'text-[10px] leading-none');
        toggleArrow.textContent = isOpen ? '▾' : '▸';
        toggle.appendChild(toggleLabel);
        toggle.appendChild(toggleArrow);

        toggle.addEventListener('click', (ev) => {
            ev.stopPropagation();
            if (expandedRows.has(filename)) expandedRows.delete(filename);
            else expandedRows.add(filename);
            if (lastDocs) updatePanel(lastDocs);
        });

        // Insert the toggle right before the download anchor so the
        // row reads: [icon] [filename + meta] ... [v3 ▸] [↓].
        const downloadAnchor = latestRow.querySelector('a');
        if (downloadAnchor) {
            latestRow.insertBefore(toggle, downloadAnchor);
        } else {
            latestRow.appendChild(toggle);
        }

        const rows = [latestRow];
        if (isOpen) {
            // Newest prior version → "v(N-1)" down to "v1" (first save).
            versions.forEach((v, idx) => {
                const versionNumber = total - 1 - idx;
                const subLabel = `v${versionNumber} · ${formatMtime(v.modifiedAt) || 'older'}`;
                const sub = buildRow(v, {
                    indented: true,
                    viewPath: `.versions/${v.filename}`,
                    label: subLabel,
                });
                if (sub) rows.push(sub);
            });
        }
        return rows;
    }

    // Last payload — kept so chevron clicks can re-render without
    // waiting for a server-emitted DOCUMENTS update.
    let lastDocs = null;

    function updatePanel(docs) {
        ensurePanel();
        if (!listEl || !countEl) return;
        lastDocs = docs;
        countEl.textContent =
            `${docs.length} file${docs.length === 1 ? '' : 's'}`;
        if (docs.length === 0) { renderEmpty(); return; }

        // Prune expanded-row keys that no longer match a current
        // filename so the set doesn't grow without bound across
        // long-lived sessions.
        const live = new Set(docs.map((d) => d && d.filename).filter(Boolean));
        for (const key of Array.from(expandedRows)) {
            if (!live.has(key)) expandedRows.delete(key);
        }

        listEl.innerHTML = '';
        for (const doc of docs) {
            for (const row of buildGroupRows(doc)) {
                listEl.appendChild(row);
            }
        }
        // List content changed; recompute alignment in case the chat
        // layout has shifted (e.g. a turn just finished + composer
        // resized).
        alignToChat();
    }

    // ── Bootstrap ────────────────────────────────────────────────────
    // Build the panel as soon as ``document.body`` exists. We don't
    // wait for DOMContentLoaded because the framework's
    // ``restoreTranscript()`` runs synchronously when ``app.js``
    // executes (defer) and may invoke this renderer first.
    const bootstrap = () => {
        ensurePanel();
        wireAlignment();
    };
    if (document.body) {
        bootstrap();
    } else if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootstrap, { once: true });
    } else {
        // ``readyState`` is ``interactive``/``complete`` but body somehow
        // missing — try again on next frame.
        requestAnimationFrame(bootstrap);
    }

    // ── Renderer ─────────────────────────────────────────────────────
    //
    // We update the side panel as a side-effect and return a tiny
    // invisible marker so the chat doesn't grow a visible "inline"
    // documents card. The marker's parent agent bubble (the
    // ``.flex.justify-start`` wrap created by app.js's
    // ``createAgentBubble``) is hidden via a precise DOM walk that
    // stops at the bubble's wrap — three levels above the marker —
    // so it can't accidentally hide siblings like the ``/hello``
    // greeting or other agent messages.
    window.A2A_RENDERERS["DOCUMENTS"] = (value) => {
        const docs = Array.isArray(value && value.documents)
            ? value.documents : [];
        updatePanel(docs);

        const marker = mkEl('span', '');
        marker.setAttribute(MARKER_ATTR, '1');
        marker.style.display = 'none';

        // After mount, walk up at most ``MAX_HOPS`` levels to find this
        // marker's own bubble wrap and hide it. Bounded so we cannot
        // walk into shared layout containers.
        const MAX_HOPS = 4;
        const hideOwnBubble = () => {
            let node = marker.parentNode;
            for (let i = 0; node && i < MAX_HOPS; i++) {
                if (
                    node.nodeType === 1 &&
                    node.classList &&
                    node.classList.contains('justify-start') &&
                    node.parentElement &&
                    node.parentElement.id === 'messages'
                ) {
                    node.style.display = 'none';
                    return;
                }
                node = node.parentElement;
            }
        };
        // ``requestAnimationFrame`` runs after the framework has
        // finished appending the marker into the bubble's text node.
        requestAnimationFrame(hideOwnBubble);
        return marker;
    };
})();
