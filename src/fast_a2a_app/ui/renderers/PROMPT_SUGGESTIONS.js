// PROMPT_SUGGESTIONS — render a
// {_type: "PROMPT_SUGGESTIONS", suggestions: [{label, prompt}, ...]}
// data part as a row of clickable pill buttons. The Python builder side
// lives in fast_a2a_app/server/artifacts/PROMPT_SUGGESTIONS.py; the two
// meet at the "_type" discriminator only.
//
// Globals it relies on (defined in the bundled UI's index.html):
//   * el(tag, classes)       — shared DOM-creation helper.
//   * sendSuggestion(prompt) — submits the click as a normal user
//                              message. Tightly coupled to the chat
//                              state machine (setBusy, runTurn, …) so
//                              it stays in index.html, not here.

window.A2A_RENDERERS = window.A2A_RENDERERS || {};

window.A2A_RENDERERS["PROMPT_SUGGESTIONS"] = (value) => {
    const suggestions = value.suggestions || [];
    const wrap = el('div', 'mt-2 flex flex-wrap gap-2');
    for (const s of suggestions) {
        const label = (s && (s.label || s.prompt)) || '';
        const prompt = (s && (s.prompt || s.label)) || '';
        if (!label || !prompt) continue;
        const btn = el('button',
            'px-3 py-1.5 text-xs bg-blue-50 text-blue-700 border border-blue-200 ' +
            'rounded-full hover:bg-blue-100 hover:border-blue-300 ' +
            'disabled:opacity-50 disabled:cursor-not-allowed transition');
        btn.textContent = label;
        btn.title = prompt;
        btn.addEventListener('click', () => {
            // Highlight the clicked pill + dim its siblings as a visual
            // breadcrumb of which option the user picked. The bubble
            // stays in chat history so the highlight persists for the
            // rest of the session — a small "I went this way" cue.
            for (const sib of wrap.querySelectorAll('button')) {
                if (sib !== btn) sib.disabled = true;
            }
            btn.classList.add('pill-selected');
            sendSuggestion(prompt);
        });
        wrap.appendChild(btn);
    }
    return wrap;
};
