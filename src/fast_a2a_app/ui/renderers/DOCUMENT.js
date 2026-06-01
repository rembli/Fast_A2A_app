// DOCUMENT — flip through a set of generated Office documents.
//
// Payload shape (built server-side by ``document_artifact`` in
// office-agent/src/artifacts.py):
//   {
//       _type: "DOCUMENT",
//       documents: [
//           {
//               filename:   "quarterly-update.pptx",
//               downloadUrl: "/download/<ctx>/quarterly-update.pptx",
//               thumbnailUrl: "/download/<ctx>/.previews/quarterly-update/slide-1.png" | null,
//               pages: [
//                   "/download/<ctx>/.previews/quarterly-update/slide-1.png",
//                   "/download/<ctx>/.previews/quarterly-update/slide-2.png",
//                   ...
//               ],
//               mediaType:  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
//               sizeBytes:  31415,
//           },
//           ...
//       ],
//   }
//
// Rendered as a single card with a big thumbnail viewer plus prev/next
// buttons, a filename caption, and a download chip. When multiple
// documents are present the user can flip between them; when there's
// only one, the navigation chrome is hidden. When ``pages`` is non-empty
// the thumbnail becomes clickable and opens a fullscreen modal that
// vertically stacks every page for native-scroll reading.
//
// This file is the office-agent example's contribution to the
// framework's renderers directory. main.py copies it into
// ``fast_a2a_app/ui/renderers/DOCUMENT.js`` at boot so
// ``build_a2a_ui`` picks it up next time it builds the served HTML.

window.A2A_RENDERERS = window.A2A_RENDERERS || {};

window.A2A_RENDERERS["DOCUMENT"] = (value) => {
    const docs = Array.isArray(value.documents) ? value.documents : [];
    const wrap = el(
        'div',
        'mt-2 rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden'
    );

    if (docs.length === 0) {
        const empty = el('div', 'px-4 py-3 text-sm text-slate-500');
        empty.textContent = 'No documents to display.';
        wrap.appendChild(empty);
        return wrap;
    }

    // ── viewer (thumbnail + filename overlay) ────────────────────────────
    const viewer = el(
        'div',
        'relative bg-slate-100 flex items-center justify-center min-h-[260px]'
    );
    const img = el(
        'img',
        'block max-h-[480px] max-w-full object-contain'
    );
    img.alt = '';
    viewer.appendChild(img);

    // Expand affordance shown when the current document has scrollable
    // pages — sits over the thumbnail in the bottom-right.
    const expandHint = el(
        'div',
        'absolute bottom-2 right-2 px-2 py-1 rounded-md bg-black/60 ' +
            'text-white text-[11px] font-medium backdrop-blur ' +
            'flex items-center gap-1 pointer-events-none'
    );
    expandHint.innerHTML = '<span>⤢</span><span>Click to expand</span>';
    expandHint.style.display = 'none';
    viewer.appendChild(expandHint);

    const placeholder = el(
        'div',
        'absolute inset-0 flex items-center justify-center text-slate-400 text-sm'
    );
    placeholder.textContent = 'No preview available';
    viewer.appendChild(placeholder);

    // Filename + index pill, top-left over the viewer.
    const titlePill = el(
        'div',
        'absolute top-2 left-2 px-2.5 py-1 rounded-md bg-black/60 ' +
            'text-white text-xs font-medium backdrop-blur'
    );
    viewer.appendChild(titlePill);

    // Size pill, top-right over the viewer.
    const sizePill = el(
        'div',
        'absolute top-2 right-2 px-2 py-0.5 rounded-md bg-white/80 ' +
            'text-slate-700 text-[11px] backdrop-blur'
    );
    viewer.appendChild(sizePill);

    wrap.appendChild(viewer);

    // ── footer (prev / next + download) ──────────────────────────────────
    const footer = el(
        'div',
        'flex items-center justify-between gap-2 px-3 py-2 border-t border-slate-200 bg-slate-50'
    );

    const navGroup = el('div', 'flex items-center gap-1');
    const prevBtn = el(
        'button',
        'px-2 py-1 rounded-md text-sm text-slate-700 hover:bg-slate-200 ' +
            'disabled:opacity-40 disabled:cursor-not-allowed'
    );
    prevBtn.type = 'button';
    prevBtn.textContent = '←';
    prevBtn.setAttribute('aria-label', 'Previous document');

    const counter = el('span', 'text-xs text-slate-600 tabular-nums px-1');

    const nextBtn = el(
        'button',
        'px-2 py-1 rounded-md text-sm text-slate-700 hover:bg-slate-200 ' +
            'disabled:opacity-40 disabled:cursor-not-allowed'
    );
    nextBtn.type = 'button';
    nextBtn.textContent = '→';
    nextBtn.setAttribute('aria-label', 'Next document');

    navGroup.appendChild(prevBtn);
    navGroup.appendChild(counter);
    navGroup.appendChild(nextBtn);
    footer.appendChild(navGroup);

    const downloadLink = el(
        'a',
        'inline-flex items-center gap-1 px-3 py-1 rounded-md ' +
            'bg-blue-600 text-white text-xs font-medium hover:bg-blue-700'
    );
    downloadLink.textContent = '↓ Download';
    downloadLink.setAttribute('download', '');
    footer.appendChild(downloadLink);
    wrap.appendChild(footer);

    // ── state + render ───────────────────────────────────────────────────
    let idx = 0;

    const formatBytes = (n) => {
        if (typeof n !== 'number' || !isFinite(n) || n < 0) return '';
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    };

    const hasPages = (doc) =>
        Array.isArray(doc && doc.pages) && doc.pages.length > 0;

    const render = () => {
        const doc = docs[idx];
        if (!doc) return;
        if (doc.thumbnailUrl) {
            img.src = doc.thumbnailUrl;
            img.style.display = '';
            placeholder.style.display = 'none';
        } else {
            img.removeAttribute('src');
            img.style.display = 'none';
            placeholder.style.display = '';
        }
        titlePill.textContent =
            docs.length > 1
                ? `${idx + 1} / ${docs.length}  •  ${doc.filename || 'document'}`
                : doc.filename || 'document';
        sizePill.textContent = formatBytes(doc.sizeBytes);
        sizePill.style.display = sizePill.textContent ? '' : 'none';
        downloadLink.href = doc.downloadUrl;
        downloadLink.setAttribute(
            'download',
            doc.filename || 'document'
        );
        counter.textContent = `${idx + 1} / ${docs.length}`;
        prevBtn.disabled = idx === 0;
        nextBtn.disabled = idx === docs.length - 1;

        // Click-to-expand affordance: only when we actually have pages.
        if (hasPages(doc)) {
            viewer.classList.add('cursor-zoom-in');
            viewer.setAttribute('role', 'button');
            viewer.setAttribute('tabindex', '0');
            viewer.setAttribute(
                'aria-label',
                `Expand ${doc.filename || 'document'} to fullscreen`
            );
            expandHint.style.display = '';
        } else {
            viewer.classList.remove('cursor-zoom-in');
            viewer.removeAttribute('role');
            viewer.removeAttribute('tabindex');
            viewer.removeAttribute('aria-label');
            expandHint.style.display = 'none';
        }
    };

    // ── fullscreen modal ────────────────────────────────────────────────
    // Constructed lazily on first open and reused across documents. The
    // modal stacks every page image vertically inside a scrollable column
    // — the browser does the scroll work, we just lay out the pages.
    let modal = null;
    let modalContent = null;
    let modalTitle = null;
    let modalCounter = null;
    let onKeyDown = null;

    const openFullscreen = () => {
        const doc = docs[idx];
        if (!hasPages(doc)) return;

        if (!modal) {
            modal = el(
                'div',
                'fixed inset-0 z-50 bg-black/80 backdrop-blur flex flex-col'
            );
            modal.setAttribute('role', 'dialog');
            modal.setAttribute('aria-modal', 'true');

            const header = el(
                'div',
                'flex items-center justify-between gap-3 px-4 py-2 ' +
                    'bg-black/60 text-white text-sm'
            );
            modalTitle = el('div', 'font-medium truncate');
            modalCounter = el('div', 'text-xs text-slate-300 tabular-nums');
            const closeBtn = el(
                'button',
                'px-3 py-1 rounded-md bg-white/10 hover:bg-white/20 ' +
                    'text-sm font-medium'
            );
            closeBtn.type = 'button';
            closeBtn.textContent = 'Close';
            closeBtn.setAttribute('aria-label', 'Close fullscreen preview');
            closeBtn.addEventListener('click', closeFullscreen);

            const titleGroup = el('div', 'flex items-center gap-3 min-w-0');
            titleGroup.appendChild(modalTitle);
            titleGroup.appendChild(modalCounter);
            header.appendChild(titleGroup);
            header.appendChild(closeBtn);
            modal.appendChild(header);

            modalContent = el(
                'div',
                'flex-1 overflow-y-auto flex flex-col items-center ' +
                    'gap-4 px-4 py-6'
            );
            modal.appendChild(modalContent);

            // Click outside any image (on the dim backdrop column) closes.
            modalContent.addEventListener('click', (event) => {
                if (event.target === modalContent) closeFullscreen();
            });
        }

        // Refill the modal for the *current* document.
        modalContent.innerHTML = '';
        for (let i = 0; i < doc.pages.length; i += 1) {
            const pageImg = el(
                'img',
                'block max-w-[min(1100px,100%)] w-full h-auto rounded shadow-lg bg-white'
            );
            pageImg.src = doc.pages[i];
            pageImg.alt = `Slide ${i + 1}`;
            pageImg.loading = i === 0 ? 'eager' : 'lazy';
            modalContent.appendChild(pageImg);
        }
        modalTitle.textContent = doc.filename || 'document';
        modalCounter.textContent = `${doc.pages.length} page${
            doc.pages.length === 1 ? '' : 's'
        }`;

        document.body.appendChild(modal);
        // Lock background scroll while the modal is open.
        modal.dataset.prevOverflow = document.body.style.overflow || '';
        document.body.style.overflow = 'hidden';
        modalContent.scrollTop = 0;

        onKeyDown = (event) => {
            if (event.key === 'Escape') closeFullscreen();
        };
        document.addEventListener('keydown', onKeyDown);
    };

    const closeFullscreen = () => {
        if (!modal || !modal.parentNode) return;
        modal.parentNode.removeChild(modal);
        document.body.style.overflow = modal.dataset.prevOverflow || '';
        if (onKeyDown) {
            document.removeEventListener('keydown', onKeyDown);
            onKeyDown = null;
        }
    };

    viewer.addEventListener('click', openFullscreen);
    viewer.addEventListener('keydown', (event) => {
        if ((event.key === 'Enter' || event.key === ' ') && hasPages(docs[idx])) {
            event.preventDefault();
            openFullscreen();
        }
    });

    prevBtn.addEventListener('click', () => {
        if (idx > 0) {
            idx -= 1;
            render();
        }
    });
    nextBtn.addEventListener('click', () => {
        if (idx < docs.length - 1) {
            idx += 1;
            render();
        }
    });

    if (docs.length < 2) {
        navGroup.style.display = 'none';
    }

    render();
    return wrap;
};
