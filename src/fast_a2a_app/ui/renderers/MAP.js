// MAP — render a {_type: "MAP", markers: [{lat, lng, label?, popup?}], ...}
// data part as an interactive Leaflet map. The Python builder lives in
// fast_a2a_app/server/artifacts/MAP.py; the two halves meet at the
// "_type" discriminator only.
//
// Leaflet is lazy-loaded from a CDN on first MAP render — chats that
// never emit a map pay no Leaflet bytes. Subsequent renders reuse the
// cached load via a module-scoped promise.

window.A2A_RENDERERS = window.A2A_RENDERERS || {};

(() => {
    const LEAFLET_VERSION = "1.9.4";
    const LEAFLET_CSS = `https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.css`;
    const LEAFLET_JS  = `https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.js`;

    let _leafletReady = null;

    function _loadLeaflet() {
        if (_leafletReady) return _leafletReady;
        _leafletReady = new Promise((resolve, reject) => {
            // CSS — fire-and-forget; the map renders without it but
            // looks unstyled until the stylesheet arrives.
            if (!document.querySelector('link[data-leaflet]')) {
                const css = document.createElement('link');
                css.rel = 'stylesheet';
                css.href = LEAFLET_CSS;
                css.dataset.leaflet = '1';
                document.head.appendChild(css);
            }
            // JS — must resolve before any L.map() call. If the global
            // ``L`` already exists (re-render after lib loaded once),
            // skip the script tag entirely.
            if (typeof window.L !== 'undefined') {
                resolve(window.L);
                return;
            }
            const script = document.createElement('script');
            script.src = LEAFLET_JS;
            script.async = true;
            script.onload = () => resolve(window.L);
            script.onerror = () => reject(new Error('failed to load Leaflet'));
            document.head.appendChild(script);
        });
        return _leafletReady;
    }

    // Plain-text escape so popup / label content (which the LLM may
    // produce) can't sneak markup into the popup. Leaflet's
    // ``bindPopup`` accepts an HTMLElement, which sidesteps the
    // string-vs-HTML ambiguity entirely.
    function _popupNode(text) {
        const node = document.createElement('div');
        node.className = 'text-xs leading-snug';
        node.textContent = String(text);
        return node;
    }

    window.A2A_RENDERERS["MAP"] = (value) => {
        const wrap = el('div',
            'mt-2 rounded-lg border border-slate-200 overflow-hidden bg-white a2a-map-wrap');

        // Widen the containing bubble so the map isn't squeezed by the
        // default 80% max-width. Walk up to the bubble div and override.
        requestAnimationFrame(() => {
            const bubble = wrap.closest('.max-w-\\[80\\%\\]');
            if (bubble) {
                bubble.style.maxWidth = '100%';
                bubble.style.width = '100%';
            }
        });

        // ── Folium HTML path: render an iframe ──────────────────────
        if (value.html_url) {
            const iframe = document.createElement('iframe');
            iframe.src = value.html_url;
            iframe.style.height = '70vh';
            iframe.style.minHeight = '400px';
            iframe.style.width = '100%';
            iframe.style.border = 'none';
            iframe.setAttribute('sandbox',
                'allow-scripts allow-same-origin');
            iframe.setAttribute('loading', 'lazy');
            wrap.appendChild(iframe);
            return wrap;
        }

        // ── Client-side Leaflet marker path ─────────────────────────
        const mapEl = el('div', '');
        // 50 vh so the map commands roughly half the viewport — large
        // enough to explore data-heavy pin sets without scrolling, but
        // still leaves room for the text reply above/below.
        mapEl.style.height = '70vh';
        mapEl.style.minHeight = '400px';
        mapEl.style.width = '100%';
        wrap.appendChild(mapEl);

        const markers = Array.isArray(value.markers) ? value.markers : [];
        const explicitCenter = Array.isArray(value.center) ? value.center : null;
        const explicitZoom = Number.isFinite(value.zoom) ? value.zoom : null;

        _loadLeaflet().then((L) => {
            // Defaults: world view if there are no usable markers, the
            // first marker if there's exactly one, auto-fit otherwise.
            const validMarkers = markers.filter(
                m => Number.isFinite(m && m.lat) && Number.isFinite(m && m.lng));
            const initialCenter = explicitCenter
                || (validMarkers.length === 1 ? [validMarkers[0].lat, validMarkers[0].lng] : [20, 0]);
            const initialZoom = explicitZoom
                ?? (validMarkers.length === 1 ? 9 : (validMarkers.length === 0 ? 2 : 4));

            const map = L.map(mapEl, {
                scrollWheelZoom: false,           // chat UX — let the page scroll
                zoomControl: true,
            }).setView(initialCenter, initialZoom);

            // Re-enable scroll-zoom once the user clicks into the map,
            // matching the "Google Maps in articles" idiom.
            map.on('click', () => map.scrollWheelZoom.enable());

            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
                maxZoom: 19,
            }).addTo(map);

            const bounds = [];
            for (const m of validMarkers) {
                const marker = L.marker([m.lat, m.lng]).addTo(map);
                if (m.label) marker.bindTooltip(String(m.label));
                if (m.popup || m.label) {
                    marker.bindPopup(_popupNode(m.popup || m.label));
                }
                bounds.push([m.lat, m.lng]);
            }
            if (!explicitCenter && bounds.length > 1) {
                map.fitBounds(bounds, { padding: [40, 40] });
            }

            // The container may not have its final dimensions when the
            // map initialises (dynamic bubble width, lazy layout). A
            // deferred invalidateSize ensures tiles fill the viewport.
            setTimeout(() => map.invalidateSize(), 120);
        }).catch((err) => {
            // Renderer must never throw — fall back to a readable
            // placeholder so the rest of the bubble still renders.
            mapEl.style.height = 'auto';
            mapEl.className = 'p-4 text-sm text-red-600';
            mapEl.textContent = `Map unavailable: ${err.message || err}`;
        });

        return wrap;
    };
})();
