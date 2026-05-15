/*
 * Overview
 * --------
 * Single-page A2A (Agent-to-Agent protocol) v1.0 chat UI. No build step, no framework —
 * everything runs in the browser directly from this file.
 *
 * How a message travels through the system:
 *   1. User types → handleSend() → sendAndStream() posts a JSON-RPC
 *      `SendStreamingMessage` request to the backend A2A endpoint.
 *   2. The server responds with a text/event-stream (SSE). Each SSE event is a
 *      JSON-RPC result containing an A2A StreamResponse
 *      (task / artifactUpdate / statusUpdate / message).
 *   3. consumeSseResponse() reads the raw byte stream, processStreamPayload()
 *      dispatches each event, and the agent bubble in the DOM is updated in real time.
 *   4. On completion the task is cleared. On network interruption the stream is
 *      re-subscribed or the task is polled as a snapshot fallback.
 *
 * Resilience strategy (why localStorage is used):
 *   - contextId persists across refreshes so the backend can correlate messages.
 *   - The active task ID is stored so an interrupted generation can be resumed
 *     on the next page load (resumeActiveTaskOnLoad).
 *   - The transcript is stored per-context so the conversation history survives
 *     a refresh without a server round-trip.
 */
'use strict';

// ── Config ───────────────────────────────────────────────────────────────────
const CARD_URL         = '/a2a/.well-known/agent-card.json';
const A2A_URL          = '/a2a/';
const CID_STORAGE_KEY  = 'a2a_context_id';
const ACTIVE_TASK_KEY  = 'a2a_active_task';
const TRANSCRIPT_KEY   = 'a2a_transcript';
const STREAM_MODE_KEY  = 'a2a_stream_mode';
const STREAM_TIMEOUT   = 300000; // ms max wait (5 min)
const A2A_HEADERS      = { 'Content-Type': 'application/json', 'A2A-Version': '1.0' };

// Server-injected runtime config (see fast_a2a_app/ui/route.py). Falsy /
// missing values disable the corresponding feature.
const UI_CONFIG = (typeof window !== 'undefined' && window.UI_CONFIG) || {};
const FILE_UPLOAD_API = (UI_CONFIG.fileUploadApi || '').trim() || null;
const ACCEPTED_FILE_TYPES = (UI_CONFIG.acceptedFileTypes || '').trim();

// ── marked setup ─────────────────────────────────────────────────────────────
marked.use({ breaks: true, gfm: true });

function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ''));
}

// ── State ────────────────────────────────────────────────────────────────────
let contextId        = localStorage.getItem(CID_STORAGE_KEY) || genUUID();
const isFreshContext = !localStorage.getItem(CID_STORAGE_KEY);
let busy             = false;
let streamController = null;
let userStopped      = false;
let streamingEnabled = localStorage.getItem(STREAM_MODE_KEY) !== 'false';
let pendingFiles     = [];   // [{ id, name, mediaType, url }]

// Fullscreen image viewer state.
// fullscreenImages is appended to as image parts (user uploads + agent
// outputs) flow through the chat in chronological order. Each entry
// may carry the ``taskId`` of the agent turn that produced it so the
// fullscreen overlay can look up the matching prompt-suggestion pills
// in ``suggestionsByTaskId``.
let fullscreenImages       = [];   // [{ raw, url, filename, mediaType, taskId? }]
let fullscreenIndex        = -1;
let fullscreenOpen         = false;
let fullscreenBusy         = false;
let fullscreenTurnHadImage = false;

// taskId → [{label, prompt}, …]. Populated whenever a streaming
// artifactUpdate carries a PROMPT_SUGGESTIONS data part; consumed by
// renderFullscreenSuggestions() when the currently-viewed fullscreen
// image came from the same task. In-memory only — page reload clears
// the map and the fullscreen overlay simply hides its pill row.
const suggestionsByTaskId  = new Map();

persist(contextId);

// ── DOM ──────────────────────────────────────────────────────────────────────
const $msgs              = document.getElementById('messages');
const $input             = document.getElementById('input');
const $sendBtn           = document.getElementById('send-btn');
const $stopBtn           = document.getElementById('stop-btn');
const $cid               = document.getElementById('cid-display');
const $streamToggle      = document.getElementById('stream-toggle');
const $streamToggleLabel = document.getElementById('stream-toggle-label');
const $attachBtn         = document.getElementById('attach-btn');
const $fileInput         = document.getElementById('file-input');
const $attachments       = document.getElementById('attachments');
const $fullscreen        = document.getElementById('fullscreen');
const $fsImg             = document.getElementById('fs-img');
const $fsCounter         = document.getElementById('fs-counter');
const $fsPrev            = document.getElementById('fs-prev');
const $fsNext            = document.getElementById('fs-next');
const $fsClose           = document.getElementById('fs-close');
const $fsLoader          = document.getElementById('fs-loader');
const $fsSuggestions     = document.getElementById('fs-suggestions');
const $fsInput           = document.getElementById('fs-input');
const $fsSend            = document.getElementById('fs-send');
const $debugConsole      = document.getElementById('debug-console');
const $tabChat           = document.getElementById('tab-chat');
const $tabDebug          = document.getElementById('tab-debug');

// ── Wire log (debug console) ────────────────────────────────────────────────
// Captures every JSON-RPC request/response that crosses the A2A wire so it
// can be inspected in the Debug tab. Bounded to the most recent N entries.
const WIRE_LOG_MAX = 200;
const wireLog = [];
let debugMode  = false;

function logWire(direction, label, payload) {
  wireLog.push({ at: new Date(), direction, label, payload });
  if (wireLog.length > WIRE_LOG_MAX) wireLog.shift();
  if (debugMode) renderDebug();
}

function fmtTime(d) {
  return d.toTimeString().slice(0, 8) + '.' + String(d.getMilliseconds()).padStart(3, '0');
}

function renderDebug() {
  const frags = wireLog.map(({ at, direction, label, payload }, idx) => {
    const arrow = direction === 'out' ? '→' : direction === 'in' ? '←' : '·';
    const cls   = direction === 'out' ? 'wire-out'
                : direction === 'in'  ? 'wire-in'
                : 'wire-error';
    let body;
    try { body = JSON.stringify(payload, null, 2); }
    catch { body = String(payload); }
    return (
      `<div class="wire-entry" data-idx="${idx}">`
      + `<div class="wire-meta"><span class="wire-toggle">▸</span><span class="${cls}">${arrow} ${esc(label)}</span> · ${fmtTime(at)}</div>`
      + `<div class="wire-body">${esc(body)}</div>`
      + `</div>`
    );
  });
  $debugConsole.innerHTML = frags.join('') || '<div class="wire-meta">No traffic yet — send a message to see the wire log.</div>';
  $debugConsole.scrollTop = $debugConsole.scrollHeight;
}

$debugConsole.addEventListener('click', (e) => {
  const meta = e.target.closest('.wire-meta');
  if (!meta) return;
  const entry = meta.parentElement;
  if (!entry || !entry.classList.contains('wire-entry')) return;
  const expanded = entry.classList.toggle('expanded');
  const toggle = meta.querySelector('.wire-toggle');
  if (toggle) toggle.textContent = expanded ? '▾' : '▸';
});

function setDebugMode(on) {
  debugMode = on;
  document.body.classList.toggle('debug-mode', on);
  $msgs.classList.toggle('hidden', on);
  $debugConsole.classList.toggle('hidden', !on);
  $tabChat.classList.toggle('active', !on);
  $tabDebug.classList.toggle('active', on);
  if (on) renderDebug();
}

// ── Initialization ───────────────────────────────────────────────────────────
refreshCid();
$streamToggle.checked = streamingEnabled;
// Hide the attach button when no upload endpoint was configured by the
// server. Without this, clicking would POST to a 404 and silently fail.
if (!FILE_UPLOAD_API) {
  $attachBtn.classList.add('hidden');
} else if (ACCEPTED_FILE_TYPES) {
  // Apply the server-configured allowlist to the native file picker so
  // the user can't even select an unsupported type. The upload endpoint
  // still validates server-side — this is just UX hygiene.
  $fileInput.accept = ACCEPTED_FILE_TYPES;
}
restoreTranscript();
fetchAgentCard().then(maybeAutoHello);
resumeActiveTaskOnLoad();

// ── Events ───────────────────────────────────────────────────────────────────
document.getElementById('new-chat-btn').addEventListener('click', () => {
  contextId = genUUID();
  persist(contextId);
  clearActiveTask();
  clearTranscript();
  clearPendingFiles();
  fullscreenImages = [];
  fullscreenIndex = -1;
  suggestionsByTaskId.clear();
  if (fullscreenOpen) closeFullscreen();
  refreshCid();
  $msgs.innerHTML = '';
  sysMsg('New conversation started.', { persist: false });
  sendSlashCommand('/hello').catch(err => errorMsg(err.message || String(err)));
});

document.getElementById('card-toggle').addEventListener('click', () => {
  document.getElementById('card-panel').classList.toggle('hidden');
});

document.getElementById('theme-toggle').addEventListener('click', () => {
  const isDark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('chatTheme', isDark ? 'dark' : 'light');
});

$tabChat.addEventListener('click',  () => setDebugMode(false));
$tabDebug.addEventListener('click', () => setDebugMode(true));

$sendBtn.addEventListener('click', handleSend);
$stopBtn.addEventListener('click', handleStop);
$streamToggle.addEventListener('change', () => {
  streamingEnabled = $streamToggle.checked;
  localStorage.setItem(STREAM_MODE_KEY, streamingEnabled);
});
$input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
});

$attachBtn.addEventListener('click', () => $fileInput.click());
$fileInput.addEventListener('change', async e => {
  const files = Array.from(e.target.files || []);
  for (const file of files) {
    try {
      const meta = await uploadImageFile(file);
      pendingFiles.push({
        id: meta.id,
        name: meta.filename || file.name,
        mediaType: meta.mediaType,
        url: meta.url,
      });
    } catch (err) {
      console.error('[a2a] failed to upload file', file.name, err);
      errorMsg(`Upload failed: ${err.message || err}`);
    }
  }
  $fileInput.value = '';
  renderAttachments();
});

$fsClose.addEventListener('click', closeFullscreen);
$fsPrev.addEventListener('click', () => navFullscreen(-1));
$fsNext.addEventListener('click', () => navFullscreen(+1));
$fsSend.addEventListener('click', handleFullscreenSend);
$fsInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleFullscreenSend(); }
});
document.addEventListener('keydown', e => {
  if (!fullscreenOpen) return;
  if (e.target === $fsInput) return;   // typing — let arrow keys move the caret
  if (e.key === 'Escape')      closeFullscreen();
  else if (e.key === 'ArrowLeft')  navFullscreen(-1);
  else if (e.key === 'ArrowRight') navFullscreen(+1);
});

// Auto-send /hello on first load when there is no transcript (cold start).
async function maybeAutoHello() {
  const hasTranscript = loadTranscript().length > 0;
  if (hasTranscript || busy) return;
  if (!isFreshContext) return;
  await sendSlashCommand('/hello').catch(() => {});
}

// ── Core flow ────────────────────────────────────────────────────────────────

async function handleSend() {
  const text = $input.value.trim();
  const files = pendingFiles.slice();
  if ((!text && files.length === 0) || busy) return;
  $input.value = '';
  clearPendingFiles();
  setBusy(true);
  userMsgParts(text, files);
  await runTurn(buildParts(text, files), thinkingMsg());
}

/** Send a slash command without rendering a user bubble. Used for /hello auto-trigger. */
async function sendSlashCommand(command) {
  if (busy) return;
  setBusy(true);
  await runTurn([{ text: command }], thinkingMsg());
}

async function runTurn(parts, thinkEl) {
  try {
    if (streamingEnabled) {
      await sendAndStream(parts, thinkEl);
    } else {
      await sendNonStreaming(parts, thinkEl);
    }
  } catch (err) {
    thinkEl.remove();
    if (err.name === 'UserStop') {
      sysMsg('Task stopped.');
    } else {
      console.error('[a2a] turn failed:', err);
      errorMsg(err.message || String(err));
    }
  } finally {
    setBusy(false);
    userStopped = false;
    streamController = null;
  }
}

function buildParts(text, files) {
  const parts = [];
  if (text) parts.push({ text });
  for (const f of files) {
    parts.push(f.url
      ? { url: f.url, filename: f.name, mediaType: f.mediaType }
      : { raw: f.base64, filename: f.name, mediaType: f.mediaType });
  }
  return parts;
}

async function handleStop() {
  if (!busy) return;
  userStopped = true;
  const activeTask = getActiveTask();
  if (activeTask?.taskId) {
    sendTaskCancel(activeTask.taskId).catch(() => {});
  }
  streamController?.abort();
}

async function sendTaskCancel(taskId) {
  const body = {
    jsonrpc: '2.0', id: genUUID(),
    method: 'CancelTask',
    params: { id: taskId },
  };
  logWire('out', body.method, body);
  await fetch(A2A_URL, {
    method: 'POST',
    headers: A2A_HEADERS,
    body: JSON.stringify(body),
  });
}

async function sendAndStream(parts, thinkEl) {
  const streamState = {
    agentBubble: null,
    completed: false,
    liveText: '',
    task: null,
    taskId: null,
    streamedArtifactCount: 0,
  };

  const sendBody = {
    jsonrpc: '2.0', id: genUUID(), method: 'SendStreamingMessage',
    params: {
      message: {
        role: 'ROLE_USER',
        messageId: genUUID(),
        contextId: contextId,
        parts: parts,
      },
    },
  };
  logWire('out', sendBody.method, sendBody);

  streamController = new AbortController();
  const timeoutId = setTimeout(() => streamController.abort(), STREAM_TIMEOUT);

  const sendResp = await fetch(A2A_URL, {
    method: 'POST',
    headers: { ...A2A_HEADERS, 'Accept': 'text/event-stream' },
    body: JSON.stringify(sendBody),
    signal: streamController.signal,
  });

  try {
    if (!sendResp.ok) throw new Error(`HTTP ${sendResp.status}`);
    if (!sendResp.body) throw new Error('Streaming response body missing.');

    await consumeSseResponse(sendResp.body, payload => {
      processStreamPayload(payload, streamState, thinkEl);
    });

    if (!streamState.completed && streamState.taskId) {
      await resubscribeTaskStream(streamState, thinkEl);
    }

    if (!streamState.completed) {
      throw new Error('Streaming connection ended before completion.');
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      if (userStopped) {
        const e = new Error('Task stopped by user.');
        e.name = 'UserStop';
        throw e;
      }
      throw new Error('Timed out waiting for streamed agent response.');
    }

    if (streamStateCanResume(err, streamState)) {
      if (streamState.taskId) {
        await resubscribeTaskStream(streamState, thinkEl, { silent: true });
        if (streamState.completed) return;
      }
    }

    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

async function sendNonStreaming(parts, thinkEl) {
  const body = {
    jsonrpc: '2.0', id: genUUID(), method: 'SendMessage',
    params: {
      message: {
        role: 'ROLE_USER',
        messageId: genUUID(),
        contextId: contextId,
        parts: parts,
      },
    },
  };
  logWire('out', body.method, body);

  const response = await fetch(A2A_URL, {
    method: 'POST',
    headers: A2A_HEADERS,
    body: JSON.stringify(body),
  });

  if (!response.ok) throw new Error(`HTTP ${response.status}`);

  const data = await response.json();
  logWire('in', 'SendMessage response', data);
  if (data.error) throw new Error(data.error.message || 'A2A error');

  thinkEl.remove();

  const result = data.result;
  if (result?.task) {
    displayTaskMessages(result.task);
  } else if (result?.message) {
    const agentText = collectTexts([result.message])
      .filter(t => !isTransientAgentText(t))
      .join('\n\n')
      .trim();
    agentMsg(agentText || '(no response)');
  } else {
    agentMsg('(no response)');
  }
}

async function consumeSseResponse(body, onPayload) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

    let boundary = findSseBoundary(buffer);
    while (boundary) {
      const rawEvent = buffer.slice(0, boundary.index);
      buffer = buffer.slice(boundary.index + boundary.length);
      processSseEvent(rawEvent, onPayload);
      boundary = findSseBoundary(buffer);
    }

    if (done) {
      if (buffer.trim()) processSseEvent(buffer, onPayload);
      return;
    }
  }
}

function processSseEvent(rawEvent, onPayload) {
  if (!rawEvent.trim()) return;

  const data = rawEvent
    .split(/\r?\n/)
    .filter(line => line.startsWith('data:'))
    .map(line => line.slice(5).trimStart())
    .join('\n')
    .trim();

  if (!data) return;

  const payload = JSON.parse(data);
  logWire('in', 'sse', payload);
  if (payload.error) {
    throw new Error(payload.error.message || 'Streaming A2A error');
  }
  onPayload(payload);
}

function findSseBoundary(buffer) {
  const match = buffer.match(/\r?\n\r?\n/);
  if (!match) return null;

  return {
    index: match.index,
    length: match[0].length,
  };
}

function processStreamPayload(payload, streamState, thinkEl) {
  const result = payload.result;
  if (!result) return;

  if (result.task) {
    streamState.task = result.task;
    streamState.taskId = result.task.id || streamState.taskId;
    persistActiveTask(streamState.taskId);
    return;
  }

  if (result.artifactUpdate) {
    const upd = result.artifactUpdate;
    streamState.task = mergeArtifactIntoTask(streamState.task, upd);
    streamState.taskId = streamState.task?.id || upd.taskId || streamState.taskId;
    if (streamState.taskId) persistActiveTask(streamState.taskId);

    // Each non-append event is a distinct artifact — give it its own bubble.
    if (!upd.append) {
      streamState.agentBubble = null;
      streamState.liveText = '';
    }

    const parts = upd.artifact?.parts || [];
    const hasRich = parts.some(p => 'data' in p || 'raw' in p || p.url);
    const artifactText = collectTexts([upd.artifact], true).join('');

    // Mixed-part artifact (caption + image/url/data) or rich-only artifact:
    // render every part in one fresh bubble, the same way the persisted
    // transcript renders it on reload. Without this, the streaming path
    // would only show ``artifactText`` and silently drop the url / data
    // sibling — which is exactly how images attached to a captioned
    // ``image_artifact`` used to disappear from the live view.
    if (!upd.append && (hasRich || !artifactText)) {
      thinkEl.remove();
      const entryId = appendTranscriptEntry('agent', artifactText, parts);
      const bubbleEl = createAgentBubble(entryId);
      renderAgentBubbleParts(bubbleEl, parts, streamState.taskId);
      streamState.streamedArtifactCount += 1;

      // If the artifact carries clickable prompt suggestions, register
      // them against the current task. The fullscreen overlay surfaces
      // them on whichever image was produced in this same turn so the
      // user can advance the conversation from the lightbox without
      // dropping back to chat.
      if (streamState.taskId) {
        recordPromptSuggestionsFromParts(streamState.taskId, parts);
      }
      return;
    }

    // Pure-text path: accumulate streamed deltas into the active bubble.
    if (upd.append) {
      streamState.liveText += artifactText;
    } else {
      streamState.liveText = artifactText;
      streamState.streamedArtifactCount += 1;
    }

    streamState.agentBubble = ensureAgentBubble(streamState.agentBubble, thinkEl);
    renderAgentBubble(streamState.agentBubble, streamState.liveText);
    return;
  }

  if (result.statusUpdate) {
    const upd = result.statusUpdate;
    streamState.task = mergeStatusIntoTask(streamState.task, upd);
    streamState.taskId = streamState.task?.id || upd.taskId || streamState.taskId;
    if (streamState.taskId) persistActiveTask(streamState.taskId);
    const state = upd.status?.state;

    if (state === 'TASK_STATE_WORKING') {
      const progressText = agentStatusText(upd);
      if (progressText && !isTransientAgentText(progressText)) {
        updateThinkingText(thinkEl, progressText);
      }
      return;
    }

    if (state === 'TASK_STATE_FAILED') {
      clearActiveTask();
      streamState.taskId = null;
      throw new Error(agentStatusText(upd) || 'Task failed on the server.');
    }
    if (state === 'TASK_STATE_CANCELED') {
      clearActiveTask();
      streamState.taskId = null;
      if (userStopped) {
        streamState.completed = true;
        return;
      }
      throw new Error('Task was canceled.');
    }

    if (state === 'TASK_STATE_COMPLETED' || state === 'TASK_STATE_INPUT_REQUIRED') {
      thinkEl.remove();

      if (!streamState.streamedArtifactCount) {
        // Nothing rendered inline — render the full task as a snapshot.
        displayTaskMessages(streamState.task || {});
      } else if (streamState.liveText && streamState.agentBubble) {
        // Last streamed artifact was text; append any rich sibling parts from
        // the same artifact (mixed-part artifact pattern).
        const artifacts = streamState.task?.artifacts || [];
        const lastArtifact = artifacts[artifacts.length - 1] || {};
        const richParts = (lastArtifact.parts || []).filter(
          p => 'data' in p || 'raw' in p || p.url
        );
        if (richParts.length > 0) {
          const card = streamState.agentBubble.parentElement;
          for (const part of richParts) {
            card.appendChild(
              'data' in part
                ? renderDataPartEl(part.data)
                : renderFilePartEl(part.raw, part.url, part.filename,
                    part.mediaType || part.media_type, streamState.taskId)
            );
          }
          if (streamState.taskId) {
            recordPromptSuggestionsFromParts(streamState.taskId, richParts);
          }
          // Persist the full part list so a page refresh restores text + rich
          // parts via renderAgentBubbleParts; without this the entry has only
          // `text` and the URL/data parts vanish on reload.
          persistAgentParts(
            streamState.agentBubble.dataset.transcriptId,
            streamState.liveText,
            lastArtifact.parts || [],
          );
          scrollEnd();
        }
      }

      streamState.completed = true;
      clearActiveTask();
    }
    return;
  }

  if (result.message) {
    const agentText = collectTexts([result.message])
      .filter(text => !isTransientAgentText(text))
      .join('\n\n');
    if (!agentText) return;

    streamState.liveText = agentText;
    streamState.agentBubble = ensureAgentBubble(
      streamState.agentBubble,
      thinkEl,
    );
    renderAgentBubble(streamState.agentBubble, streamState.liveText);
  }
}

async function resubscribeTaskStream(streamState, thinkEl, options = {}) {
  if (!streamState.taskId) return;

  const resumeBody = {
    jsonrpc: '2.0',
    id: genUUID(),
    method: 'SubscribeToTask',
    params: { id: streamState.taskId },
  };
  logWire('out', resumeBody.method, resumeBody);

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), STREAM_TIMEOUT);

  try {
    const response = await fetch(A2A_URL, {
      method: 'POST',
      headers: { ...A2A_HEADERS, 'Accept': 'text/event-stream' },
      body: JSON.stringify(resumeBody),
      signal: controller.signal,
    });

    if (!response.ok) {
      await displayTaskSnapshot(streamState.taskId, streamState, thinkEl);
      return;
    }
    if (!response.body) {
      throw new Error('Streaming response body missing.');
    }

    if (!options.silent) {
      sysMsg('Resuming interrupted stream...', { persist: false });
    }

    await consumeSseResponse(response.body, payload => {
      processStreamPayload(payload, streamState, thinkEl);
    });

    if (!streamState.completed) {
      await displayTaskSnapshot(streamState.taskId, streamState, thinkEl);
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      await displayTaskSnapshot(streamState.taskId, streamState, thinkEl);
      return;
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

async function displayTaskSnapshot(taskId, streamState, thinkEl) {
  const task = await fetchTask(taskId);
  if (!task) return;

  streamState.task = task;
  streamState.taskId = task.id || taskId;

  const state = task.status?.state;
  if (state === 'TASK_STATE_FAILED') {
    clearActiveTask();
    throw new Error(agentStatusText({ status: task.status }) || 'Task failed on the server.');
  }
  if (state === 'TASK_STATE_CANCELED') {
    clearActiveTask();
    throw new Error('Task was canceled.');
  }

  thinkEl.remove();
  displayTaskMessages(task, streamState.agentBubble);

  if (state === 'TASK_STATE_COMPLETED' || state === 'TASK_STATE_INPUT_REQUIRED') {
    streamState.completed = true;
    clearActiveTask();
  }
}

async function fetchTask(taskId) {
  const body = {
    jsonrpc: '2.0',
    id: genUUID(),
    method: 'GetTask',
    params: { id: taskId },
  };
  logWire('out', body.method, body);
  const response = await fetch(A2A_URL, {
    method: 'POST',
    headers: A2A_HEADERS,
    body: JSON.stringify(body),
  });
  if (!response.ok) return null;

  const payload = await response.json();
  logWire('in', 'GetTask response', payload);
  if (payload.error) return null;
  return payload.result || null;
}

async function resumeActiveTaskOnLoad() {
  const activeTask = getActiveTask();
  if (!activeTask || activeTask.contextId !== contextId || busy) return;

  const snapshot = await fetchTask(activeTask.taskId).catch(() => null);
  if (!snapshot) { clearActiveTask(); return; }
  const snapState = snapshot.status?.state;
  if (snapState === 'TASK_STATE_FAILED' || snapState === 'TASK_STATE_CANCELED') {
    clearActiveTask();
    return;
  }
  if (snapState === 'TASK_STATE_COMPLETED' || snapState === 'TASK_STATE_INPUT_REQUIRED') {
    clearActiveTask();
    return;
  }

  setBusy(true);
  sysMsg('Resuming previous response...', { persist: false });
  const thinkEl = thinkingMsg();
  const lastEntry = getLastTranscriptEntry();
  const streamState = {
    agentBubble: lastEntry?.role === 'agent'
      ? findTranscriptBubble(lastEntry.id)
      : null,
    completed: false,
    liveText: lastEntry?.role === 'agent' ? lastEntry.text : '',
    task: null,
    taskId: activeTask.taskId,
    streamedArtifactCount: 0,
  };

  try {
    await resubscribeTaskStream(streamState, thinkEl, { silent: true });
  } catch (err) {
    console.error('[a2a] resume failed:', err);
    thinkEl.remove();
    errorMsg(err.message || String(err));
  } finally {
    setBusy(false);
  }
}

// ── Task persistence ──────────────────────────────────────────────────────────

function persistActiveTask(taskId) {
  if (!taskId) return;
  localStorage.setItem(ACTIVE_TASK_KEY, JSON.stringify({
    contextId,
    taskId,
  }));
}

function getActiveTask() {
  const raw = localStorage.getItem(ACTIVE_TASK_KEY);
  if (!raw) return null;

  try {
    return JSON.parse(raw);
  } catch {
    clearActiveTask();
    return null;
  }
}

function clearActiveTask() {
  localStorage.removeItem(ACTIVE_TASK_KEY);
}

// ── Transcript persistence ────────────────────────────────────────────────────

function transcriptStorageKey() {
  return `${TRANSCRIPT_KEY}:${contextId}`;
}

function loadTranscript() {
  const raw = localStorage.getItem(transcriptStorageKey());
  if (!raw) return [];

  try {
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data : [];
  } catch {
    clearTranscript();
    return [];
  }
}

function saveTranscript(entries) {
  localStorage.setItem(transcriptStorageKey(), JSON.stringify(entries));
}

/**
 * Append an entry. `parts` (optional) is the raw A2A parts array for multi-part
 * agent responses — stored so the bubble can be re-rendered on page refresh.
 */
function appendTranscriptEntry(role, text, parts = null) {
  const entries = loadTranscript();
  const entry = { id: genUUID(), role, text };
  if (parts) entry.parts = parts;
  entries.push(entry);
  saveTranscript(entries);
  return entry.id;
}

function updateTranscriptEntry(entryId, text) {
  if (!entryId) return;

  const entries = loadTranscript();
  saveTranscript(entries.map(entry => (
    entry.id === entryId ? { ...entry, text } : entry
  )));
}

/**
 * Replace text + parts on an existing agent entry. Used at task completion
 * for mixed-part artifacts (text caption + url/data/raw rich parts) where
 * the streaming path persisted only the text — without the parts here, a
 * page refresh would restore the bubble as text-only.
 */
function persistAgentParts(entryId, text, parts) {
  if (!entryId) return;
  const entries = loadTranscript();
  saveTranscript(entries.map(entry => (
    entry.id === entryId ? { ...entry, text, parts } : entry
  )));
}

function getLastTranscriptEntry() {
  const entries = loadTranscript();
  return entries.length > 0 ? entries[entries.length - 1] : null;
}

function clearTranscript() {
  localStorage.removeItem(transcriptStorageKey());
}

function restoreTranscript() {
  const entries = loadTranscript();
  if (entries.length === 0) return;

  $msgs.innerHTML = '';
  for (const entry of entries) {
    renderStoredEntry(entry);
  }
}

function renderStoredEntry(entry) {
  if (entry.role === 'user') {
    if (entry.parts) {
      const files = entry.parts
        .filter(p => 'raw' in p || 'url' in p)
        .map(p => ({
          id: genUUID(),
          name: p.filename || 'file',
          mediaType: p.mediaType || p.media_type || 'application/octet-stream',
          base64: p.raw || null,
          url: p.url || null,
          dataUrl: '',
        }));
      userMsgParts(entry.text, files, { persist: false });
    } else {
      userMsg(entry.text, { persist: false });
    }
    return;
  }
  if (entry.role === 'agent') {
    if (entry.parts) {
      const contentEl = createAgentBubble(entry.id);
      renderAgentBubbleParts(contentEl, entry.parts);
    } else {
      agentMsg(entry.text, { persist: false, entryId: entry.id });
    }
    return;
  }
  if (entry.role === 'system') {
    sysMsg(entry.text, { persist: false });
    return;
  }
  if (entry.role === 'error') {
    errorMsg(entry.text, { persist: false });
  }
}

function findTranscriptBubble(entryId) {
  return Array.from(document.querySelectorAll('[data-transcript-id]'))
    .find(element => element.dataset.transcriptId === entryId) || null;
}

// ── Stream state helpers ──────────────────────────────────────────────────────

function streamStateCanResume(err, streamState) {
  return Boolean(err && err.name !== 'AbortError' && streamState.taskId);
}

function mergeStatusIntoTask(task, event) {
  const nextTask = task ? { ...task } : { kind: 'task' };
  nextTask.id = nextTask.id || event.taskId;
  nextTask.contextId = nextTask.contextId || event.contextId;
  nextTask.status = event.status;
  if (event.status?.message) {
    nextTask.history = [...(nextTask.history || []), event.status.message];
  }
  return nextTask;
}

function mergeArtifactIntoTask(task, event) {
  const nextTask = task ? { ...task } : { kind: 'task' };
  nextTask.id = nextTask.id || event.taskId;
  nextTask.contextId = nextTask.contextId || event.contextId;
  const artifacts = [...(nextTask.artifacts || [])];

  if (event.append && artifacts.length > 0) {
    const lastArtifact = { ...artifacts[artifacts.length - 1] };
    lastArtifact.parts = [
      ...(lastArtifact.parts || []),
      ...(event.artifact?.parts || []),
    ];
    artifacts[artifacts.length - 1] = lastArtifact;
  } else if (event.artifact) {
    artifacts.push(event.artifact);
  }

  nextTask.artifacts = artifacts;
  return nextTask;
}

function ensureAgentBubble(existingBubble, thinkEl) {
  if (existingBubble) return existingBubble;
  thinkEl.remove();

  return createAgentBubble(appendTranscriptEntry('agent', ''));
}

function renderAgentBubble(bubble, text) {
  bubble.innerHTML = renderMarkdown(text || '(no response)');
  updateTranscriptEntry(
    bubble.dataset.transcriptId,
    text || '(no response)'
  );
  scrollEnd();
}

function agentStatusText(event) {
  return collectTexts([event.status?.message || {}]).join('\n\n');
}

/**
 * Renders the completed task response. Handles three cases:
 *   1. No artifact parts → fall back to agent history text
 *   2. Text-only parts   → existing markdown bubble
 *   3. Mixed parts       → multi-part bubble (text + data + file widgets)
 */
function displayTaskMessages(task, existingBubble = null) {
  const allParts = (task.artifacts || []).flatMap(a => a.parts || []);

  // No artifact parts — fall back to history
  if (allParts.length === 0) {
    const historyTexts = collectTexts(
      (task.history || []).filter(msg => msg.role === 'ROLE_AGENT')
    ).filter(text => !isTransientAgentText(text));

    const responseText = uniqueTexts(historyTexts).join('\n\n');
    if (!responseText) {
      if (existingBubble) { renderAgentBubble(existingBubble, '(no response)'); return; }
      agentMsg('(no response)');
      return;
    }
    if (existingBubble) { renderAgentBubble(existingBubble, responseText); return; }
    agentMsg(responseText);
    return;
  }

  const richPresent = allParts.some(p => 'data' in p || 'raw' in p || p.url);
  // Chunks of the *same* artifact (e.g. streamed text fragments) concatenate
  // verbatim — the streaming layer already inserted whatever inter-chunk
  // spacing it wanted. Only the boundaries between *distinct* artifacts get
  // a paragraph break, so a multi-chunk response doesn't render as one
  // <p> per chunk (which is what made each word appear on its own line for
  // word-streaming agents like echo_agent under non-streaming mode).
  const textContent = (task.artifacts || [])
    .map(a => (a.parts || [])
      .filter(p => typeof p.text === 'string' && p.text)
      .map(p => p.text)
      .join('')
    )
    .map(t => t.trim())
    .filter(Boolean)
    .join('\n\n');

  if (!richPresent) {
    // Text-only artifact
    const responseText = textContent || '(no response)';
    if (existingBubble) { renderAgentBubble(existingBubble, responseText); return; }
    agentMsg(responseText);
    return;
  }

  // Multi-part
  if (existingBubble) {
    renderAgentBubbleParts(existingBubble, allParts, task.id || null);
    persistAgentParts(existingBubble.dataset.transcriptId, textContent, allParts);
  } else {
    agentMsgParts(allParts, textContent, task.id || null);
  }
  // Pick up any closing PROMPT_SUGGESTIONS the snapshot includes so the
  // fullscreen overlay can surface them on this turn's images.
  if (task.id) {
    recordPromptSuggestionsFromParts(task.id, allParts);
  }
}

// ── Agent card ────────────────────────────────────────────────────────────────

/**
 * Fetches the agent card and:
 *   - populates the header and info panel
 *   - shows the stream toggle only when capabilities.streaming is true
 */
async function fetchAgentCard() {
  try {
    const card = await (await fetch(CARD_URL)).json();
    document.getElementById('agent-name').textContent = card.name || 'Agent';
    document.getElementById('agent-desc').textContent = card.description || '';
    document.title = card.name || 'Agent';
    renderCard(card);

    if (card.capabilities?.streaming) {
      $streamToggleLabel.classList.remove('hidden');
      $streamToggleLabel.style.display = 'flex';
    } else {
      // Agent only supports non-streaming; disable silently.
      streamingEnabled = false;
    }
  } catch (err) {
    console.error('[a2a] fetchAgentCard failed:', err);
  }
}

function renderCard(card) {
  const rows = [];
  if (card.version) rows.push(`<b>Version:</b> ${esc(card.version)}`);
  if (card.url)     rows.push(`<b>URL:</b> ${esc(card.url)}`);
  if (card.capabilities) {
    const caps = Object.entries(card.capabilities)
      .filter(([, v]) => v).map(([k]) => k).join(', ');
    if (caps) rows.push(`<b>Capabilities:</b> ${esc(caps)}`);
  }
  if (card.skills?.length) {
    rows.push('<b>Skills:</b>');
    card.skills.forEach(s => rows.push(
      `&nbsp;&nbsp;• <b>${esc(s.name)}</b>${s.description ? ': ' + esc(s.description) : ''}`
    ));
  }
  document.getElementById('card-body').innerHTML = rows.join('<br>');
}

// ── Message renderers ─────────────────────────────────────────────────────────

function userMsg(text, options = {}) {
  if (options.persist !== false) {
    appendTranscriptEntry('user', text);
  }
  append('flex justify-end', `
    <div class="max-w-[75%] bg-blue-600 text-white rounded-2xl rounded-tr-sm
                px-4 py-2.5 text-sm whitespace-pre-wrap">${esc(text)}</div>`);
}

/**
 * Render a user bubble that may contain text + file attachments.
 * Image parts are rendered as click-to-zoom thumbnails; non-image
 * parts (.csv, .xlsx, .pdf, …) get a generic file tile with the
 * type icon + filename. Attachments are persisted as parts so the
 * bubble survives a refresh.
 */
function userMsgParts(text, files, options = {}) {
  if (!files || files.length === 0) {
    if (text) userMsg(text, options);
    return;
  }
  const parts = [];
  if (text) parts.push({ text });
  for (const f of files) {
    const part = f.url
      ? { url: f.url, filename: f.name, mediaType: f.mediaType }
      : { raw: f.base64, filename: f.name, mediaType: f.mediaType };
    parts.push(part);
  }
  if (options.persist !== false) {
    appendTranscriptEntry('user', text || '', parts);
  }

  const wrap = el('div', 'flex justify-end');
  const bubble = el('div',
    'max-w-[80%] bg-blue-600 text-white rounded-2xl rounded-tr-sm px-3 py-2.5 text-sm');
  if (files.length > 0) {
    const grid = el('div', 'flex flex-wrap gap-2 mb-1.5');
    for (const f of files) {
      let onImageClick = null;
      if ((f.mediaType || '').startsWith('image/')) {
        const idx = trackImagePart({
          raw: f.base64, url: f.url, filename: f.name, mediaType: f.mediaType,
        });
        if (idx >= 0) onImageClick = () => openFullscreen(idx);
      }
      grid.appendChild(attachmentTile(f, {
        sizeClass: 'w-20 h-20',
        showName: true,
        onImageClick,
      }));
    }
    bubble.appendChild(grid);
  }
  if (text) {
    const t = el('div', 'whitespace-pre-wrap');
    t.textContent = text;
    bubble.appendChild(t);
  }
  wrap.appendChild(bubble);
  $msgs.appendChild(wrap);
  scrollEnd();
}

// ── Attachments ──────────────────────────────────────────────────────────────

function renderAttachments() {
  $attachments.innerHTML = '';
  if (pendingFiles.length === 0) {
    $attachments.classList.add('hidden');
    return;
  }
  $attachments.classList.remove('hidden');
  for (const f of pendingFiles) {
    const tile = attachmentTile(f, { sizeClass: 'w-16 h-16', showName: false });
    const remove = el('button',
      'absolute top-0.5 right-0.5 bg-black/60 text-white rounded-full w-5 h-5 ' +
      'flex items-center justify-center text-xs leading-none ' +
      'opacity-0 group-hover:opacity-100 transition');
    remove.textContent = '×';
    remove.title = 'Remove';
    remove.addEventListener('click', () => {
      pendingFiles = pendingFiles.filter(p => p.id !== f.id);
      renderAttachments();
    });
    tile.appendChild(remove);
    $attachments.appendChild(tile);
  }
}

function clearPendingFiles() {
  pendingFiles = [];
  renderAttachments();
}

// ── Fullscreen image viewer ──────────────────────────────────────────────────

/**
 * Append an image part to the navigable list. Called from every code path
 * that renders an image into the chat (live stream, transcript restore,
 * user uploads, agent outputs). Returns the new index so callers can wire
 * a click handler that opens fullscreen at this exact image.
 *
 * Either ``raw`` (base64 bytes) or ``url`` (resolvable URL) must be set.
 * Agent outputs typically arrive as URLs (image_store-backed); user uploads
 * arrive as raw bytes from the file picker.
 *
 * If a fullscreen turn is in flight and waiting for the agent's new image,
 * jump to the freshly-arrived image and hide the loader.
 */
function trackImagePart({ raw, url, filename, mediaType, taskId }) {
  if (!raw && !url) return -1;
  const idx = fullscreenImages.length;
  fullscreenImages.push({
    raw: raw || null,
    url: url || null,
    filename: filename || 'image.png',
    mediaType: mediaType || 'image/png',
    taskId: taskId || null,
  });

  if (fullscreenOpen && fullscreenBusy && !fullscreenTurnHadImage) {
    fullscreenTurnHadImage = true;
    fullscreenIndex = idx;
    setFullscreenLoading(false);
    renderFullscreenImage();
    renderFullscreenSuggestions();
  }
  return idx;
}

function openFullscreen(index) {
  if (!fullscreenImages.length) return;
  fullscreenIndex = Math.max(0, Math.min(index, fullscreenImages.length - 1));
  fullscreenOpen = true;
  $fullscreen.classList.remove('hidden');
  renderFullscreenImage();
  renderFullscreenSuggestions();
  // Show the spinner whenever a turn is in flight, regardless of where it
  // was initiated — except after a fullscreen-initiated turn has already
  // swapped in its result image (then we don't want to re-hide it).
  setFullscreenLoading(busy && !(fullscreenBusy && fullscreenTurnHadImage));
  $fsInput.value = '';
  $fsInput.focus();
}

function closeFullscreen() {
  fullscreenOpen = false;
  $fullscreen.classList.add('hidden');
  // Note: fullscreenBusy / fullscreenTurnHadImage are owned by the turn
  // lifecycle (set in handleFullscreenSend, cleared in its finally block).
  // Don't reset them here — preserving them lets the user reopen
  // fullscreen mid-turn and resume the loader / pick up the new image.
}

function navFullscreen(delta) {
  if (!fullscreenOpen || fullscreenImages.length === 0) return;
  const next = fullscreenIndex + delta;
  if (next < 0 || next >= fullscreenImages.length) return;
  fullscreenIndex = next;
  renderFullscreenImage();
  renderFullscreenSuggestions();
}

function renderFullscreenImage() {
  const item = fullscreenImages[fullscreenIndex];
  if (!item) {
    $fsImg.src = '';
    $fsCounter.textContent = '0 / 0';
    $fsPrev.disabled = $fsNext.disabled = true;
    return;
  }
  $fsImg.src = item.url || `data:${item.mediaType};base64,${item.raw}`;
  $fsImg.alt = item.filename || 'image';
  $fsCounter.textContent = `${fullscreenIndex + 1} / ${fullscreenImages.length}`;
  $fsPrev.disabled = fullscreenIndex === 0;
  $fsNext.disabled = fullscreenIndex === fullscreenImages.length - 1;
}

// Spinner overlay only — the input/send disabled state is owned by
// setBusy() so it stays consistent with the main composer regardless of
// whether the in-flight turn was initiated from fullscreen or the chat.
function setFullscreenLoading(loading) {
  $fsLoader.classList.toggle('hidden', !loading);
}

/**
 * Scan a part list for a ``{_type: "PROMPT_SUGGESTIONS"}`` data part and,
 * if present, register its ``suggestions`` array against ``taskId`` so
 * ``renderFullscreenSuggestions()`` can surface them when an image from
 * the same task is currently being viewed.
 *
 * Idempotent — re-recording the same task with the same suggestions is
 * a no-op. If multiple PROMPT_SUGGESTIONS arrive in one turn (rare),
 * the last one wins.
 */
function recordPromptSuggestionsFromParts(taskId, parts) {
  if (!taskId || !Array.isArray(parts)) return;
  for (const part of parts) {
    if (!('data' in part) || !part.data) continue;
    const data = part.data;
    if (data._type !== 'PROMPT_SUGGESTIONS') continue;
    const list = Array.isArray(data.suggestions) ? data.suggestions : [];
    if (list.length === 0) continue;
    suggestionsByTaskId.set(taskId, list);
    // If the user is currently viewing an image from this turn, the new
    // pills should appear immediately rather than waiting for a nav.
    if (fullscreenOpen) {
      const cur = fullscreenImages[fullscreenIndex];
      if (cur && cur.taskId === taskId) renderFullscreenSuggestions();
    }
  }
}

/**
 * Render the prompt-suggestion pills for the *currently-selected*
 * fullscreen image. Looks up by the image's ``taskId`` so navigating
 * prev/next surfaces the matching turn's suggestions (or hides the
 * row when an image has none — e.g. user uploads).
 */
function renderFullscreenSuggestions() {
  $fsSuggestions.innerHTML = '';
  const item = fullscreenImages[fullscreenIndex];
  const list = item && item.taskId ? suggestionsByTaskId.get(item.taskId) : null;
  if (!list || list.length === 0) {
    $fsSuggestions.classList.add('hidden');
    return;
  }
  $fsSuggestions.classList.remove('hidden');
  for (const s of list) {
    const label = (s && (s.label || s.prompt)) || '';
    const prompt = (s && (s.prompt || s.label)) || '';
    if (!label || !prompt) continue;
    const btn = el('button',
      'px-3 py-1.5 text-xs bg-white/10 text-white border border-white/25 ' +
      'rounded-full hover:bg-white/20 hover:border-white/40 ' +
      'disabled:opacity-40 disabled:cursor-not-allowed transition');
    btn.textContent = label;
    btn.title = prompt;
    if (busy || fullscreenBusy) btn.disabled = true;
    btn.addEventListener('click', () => {
      // Highlight the pill the user picked + grey out its siblings so
      // it's visually clear which next-step is in flight. Re-renders
      // (next turn / nav) start fresh, so the class doesn't need
      // explicit cleanup.
      for (const sib of $fsSuggestions.querySelectorAll('button')) {
        if (sib !== btn) sib.disabled = true;
      }
      btn.classList.add('pill-selected');
      sendFullscreenSuggestion(prompt);
    });
    $fsSuggestions.appendChild(btn);
  }
}

/**
 * Send a suggestion's prompt as the next user message from the
 * fullscreen view — the currently-viewed image rides along as a
 * reference part, same as if the user had typed the prompt into
 * the fs-input bar.
 */
async function sendFullscreenSuggestion(promptText) {
  if (!promptText || busy || fullscreenBusy) return;
  $fsInput.value = promptText;
  await handleFullscreenSend();
}

/**
 * Send a prompt from the fullscreen view, including the currently-viewed
 * image as a reference part. Behaviour after the turn:
 *   - if the agent produced a new image during the turn → fullscreen stays
 *     open, pointing at that new image (handled in trackImagePart);
 *   - otherwise the fullscreen closes so the user sees the text response
 *     in the underlying chat.
 */
async function handleFullscreenSend() {
  const text = $fsInput.value.trim();
  if (!text || fullscreenBusy || busy) return;
  if (fullscreenIndex < 0 || fullscreenIndex >= fullscreenImages.length) return;

  const current = fullscreenImages[fullscreenIndex];
  $fsInput.value = '';

  // Build the reference part: url-only when the agent stored it server-side,
  // raw bytes for items the user uploaded that aren't yet stored.
  const refPart = current.url
    ? { url: current.url, filename: current.filename, mediaType: current.mediaType }
    : { raw: current.raw, filename: current.filename, mediaType: current.mediaType };

  // Render the user message in the chat behind so the transcript stays accurate.
  userMsgParts(text, [{
    id: genUUID(),
    name: current.filename,
    mediaType: current.mediaType,
    base64: current.raw || null,
    url: current.url || null,
    dataUrl: '',
  }]);

  fullscreenBusy = true;
  fullscreenTurnHadImage = false;
  setFullscreenLoading(true);
  setBusy(true);

  const parts = [{ text }, refPart];
  try {
    await runTurn(parts, thinkingMsg());
  } finally {
    fullscreenBusy = false;
    setFullscreenLoading(false);
    if (fullscreenOpen && !fullscreenTurnHadImage) {
      // Text-only response (or error) — return to the chat so the user sees it.
      closeFullscreen();
    }
  }
}

/**
 * POST a file to the agent's configured upload endpoint (FILE_UPLOAD_API)
 * and return the metadata record ``{ id, url, mediaType, filename }``.
 * Pre-uploading on file-pick keeps base64 image bytes out of the chat's
 * localStorage transcript so a page refresh doesn't lose recently-uploaded
 * references.
 *
 * Throws if upload isn't configured — callers should normally guard via
 * the hidden attach button instead, but the check is here as a backstop.
 */
async function uploadImageFile(file) {
  if (!FILE_UPLOAD_API) {
    throw new Error('File upload not configured (FILE_UPLOAD_API is unset).');
  }
  const fd = new FormData();
  fd.append('file', file, file.name);
  const resp = await fetch(FILE_UPLOAD_API, { method: 'POST', body: fd });
  if (!resp.ok) {
    let detail = '';
    try { detail = (await resp.json()).detail || ''; } catch { /* ignore */ }
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function agentMsg(text, options = {}) {
  const entryId = options.entryId || appendTranscriptEntry('agent', text);
  const textEl = createAgentBubble(entryId);
  renderAgentBubble(textEl, text);
}

/** Creates a multi-part agent bubble and persists the parts to the transcript. */
function agentMsgParts(parts, textForTranscript = '', taskId = null) {
  const entryId = appendTranscriptEntry('agent', textForTranscript, parts);
  const contentEl = createAgentBubble(entryId);
  renderAgentBubbleParts(contentEl, parts, taskId);
}

function updateThinkingText(thinkEl, text) {
  if (!thinkEl?.isConnected) return;
  const span = thinkEl.querySelector('.italic');
  if (span) span.textContent = text;
}

function thinkingMsg() {
  const wrap = el('div', 'flex justify-start');
  wrap.innerHTML = `
    <div class="bg-white border border-slate-200 rounded-2xl rounded-tl-sm
                px-4 py-3 shadow-sm text-sm text-slate-400 flex items-center gap-2">
      <span class="spinner"></span>
      <span class="italic">Working…</span>
    </div>`;
  $msgs.appendChild(wrap);
  scrollEnd();
  return wrap;
}

function sysMsg(text, options = {}) {
  if (options.persist !== false) {
    appendTranscriptEntry('system', text);
  }
  append('flex justify-center',
    `<span class="text-xs text-slate-400 bg-slate-100 px-3 py-1 rounded-full">${esc(text)}</span>`);
}

function errorMsg(text, options = {}) {
  if (options.persist !== false) {
    appendTranscriptEntry('error', text);
  }
  append('flex justify-start', `
    <div class="max-w-[75%] bg-red-50 border border-red-200 text-red-700
                rounded-2xl px-4 py-2.5 text-sm">⚠ ${esc(text)}</div>`);
}

function createAgentBubble(entryId = '') {
  const wrap = el('div', 'flex justify-start');
  const bubble = el('div',
    'max-w-[80%] bg-white border border-slate-200 rounded-2xl ' +
    'rounded-tl-sm px-4 py-3 shadow-sm');
  const textEl = el('div', 'text-sm text-slate-800 leading-relaxed markdown');
  textEl.dataset.transcriptId = entryId;
  bubble.appendChild(textEl);
  wrap.appendChild(bubble);
  $msgs.appendChild(wrap);
  scrollEnd();
  return textEl;
}

// ── Part renderers ────────────────────────────────────────────────────────────

/**
 * Populates `contentEl` with one child element per part:
 *   text → markdown div
 *   data → key-value / typed-renderer widget
 *   raw/url → file download card (image media types render inline)
 *
 * ``taskId`` (optional) is threaded down into renderFilePartEl so any
 * image part it renders can be tagged in ``fullscreenImages`` with the
 * task it originated from — that's what powers the fullscreen overlay's
 * per-image prompt-suggestion lookup. Restore-from-localStorage paths
 * may not have a taskId and pass ``null`` here; that's fine, the image
 * just won't carry one.
 */
function renderAgentBubbleParts(contentEl, parts, taskId = null) {
  contentEl.innerHTML = '';
  contentEl.classList.remove('markdown');
  let first = true;
  for (const part of parts) {
    if (part.text) {
      const d = el('div', 'markdown text-sm text-slate-800 leading-relaxed' + (first ? '' : ' mt-2'));
      d.innerHTML = renderMarkdown(part.text);
      contentEl.appendChild(d);
      first = false;
    } else if ('data' in part) {
      contentEl.appendChild(renderDataPartEl(part.data));
      first = false;
    } else if ('raw' in part || part.url) {
      const mt = part.mediaType || part.media_type || '';
      if ('raw' in part && mt.includes('json')) {
        // Render JSON raw parts as a key-value table, not a download card.
        try {
          const bytes = Uint8Array.from(atob(part.raw), c => c.charCodeAt(0));
          const parsed = JSON.parse(new TextDecoder().decode(bytes));
          contentEl.appendChild(renderDataPartEl(parsed));
        } catch {
          contentEl.appendChild(renderFilePartEl(part.raw, part.url, part.filename, mt, taskId));
        }
      } else {
        contentEl.appendChild(renderFilePartEl(part.raw, part.url, part.filename, mt, taskId));
      }
      first = false;
    }
  }
  scrollEnd();
}

/**
 * Renders a google.protobuf.Value (arrived as a plain JSON value).
 *
 * Typed envelopes ({"_type": "TAG", ...}) are dispatched through
 * window.A2A_RENDERERS — the registry of typed renderers populated
 * by the snippet ``ArtifactTypeRegistry.js_snippet()`` injects at
 * page-load time. Built-ins (``TABLE``, ``PROMPT_SUGGESTIONS``) and
 * any application-supplied tags share the same lookup, so adding a
 * new type is one ``artifact_types.register(...)`` call on the
 * Python side.
 *
 * Anything without a matching renderer falls through to the generic
 * key-value rendering below — flat objects get one row per key,
 * everything else becomes a compact JSON block.
 */
function renderDataPartEl(value) {
  if (value && typeof value === 'object' && typeof value._type === 'string') {
    const renderer = window.A2A_RENDERERS && window.A2A_RENDERERS[value._type];
    if (typeof renderer === 'function') {
      return renderer(value);
    }
  }

  const wrap = el('div', 'mt-2 rounded-lg border border-slate-200 overflow-hidden text-xs font-mono');

  // Header
  const header = el('div', 'px-3 py-1.5 bg-slate-100 border-b border-slate-200 flex items-center gap-2');
  const icon = el('span', 'text-slate-400 text-[11px] font-bold');
  icon.textContent = '{ }';
  const label = el('span', 'text-[10px] uppercase tracking-widest text-slate-400 font-medium font-sans');
  label.textContent = 'data';
  header.appendChild(icon);
  header.appendChild(label);
  wrap.appendChild(header);

  const body = el('div', 'px-3 py-2 bg-white');

  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    const table = el('table', 'w-full');
    for (const [k, v] of Object.entries(value)) {
      const tr = el('tr', '');
      const tdKey = el('td', 'kv-key py-0.5');
      tdKey.textContent = k;
      const cls = kvClass(v);
      const tdVal = el('td', `kv-${cls} py-0.5`);
      tdVal.textContent = kvFormat(v);
      tr.appendChild(tdKey);
      tr.appendChild(tdVal);
      table.appendChild(tr);
    }
    body.appendChild(table);
  } else {
    const pre = el('pre', 'text-slate-700 whitespace-pre-wrap break-all text-xs');
    pre.textContent = JSON.stringify(value, null, 2);
    body.appendChild(pre);
  }

  wrap.appendChild(body);
  return wrap;
}

// ── Specialised renderers ────────────────────────────────────────────────────
//
// The renderers for typed data parts (``TABLE``, ``PROMPT_SUGGESTIONS``)
// live in their respective Python modules under
// ``fast_a2a_app/server/artifacts/`` and are inlined into this page at
// build time by ``build_a2a_ui`` via ``window.A2A_RENDERERS``. Only
// the standard / embedded artifact rendering (text, data key-value
// fallback, file/image) lives in this file.
//
// One chat-state-bound helper is kept here because the renderer for
// ``PROMPT_SUGGESTIONS`` references it as a global — moving it into the
// artifact module would drag along the whole chat-flow surface
// (``setBusy``, ``runTurn``, ``userMsgParts``, ``thinkingMsg``).
async function sendSuggestion(prompt) {
  if (!prompt || busy) return;
  setBusy(true);
  userMsgParts(prompt, []);
  await runTurn([{ text: prompt }], thinkingMsg());
}

function kvClass(v) {
  if (v === null) return 'null';
  if (typeof v === 'string')  return 'str';
  if (typeof v === 'number')  return 'num';
  if (typeof v === 'boolean') return 'bool';
  return 'obj';
}

function kvFormat(v) {
  if (v === null) return 'null';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/**
 * Renders a file part as a download card with a type icon, filename,
 * media type label, and a download button that creates a temporary Blob URL.
 * `raw` is a base64 string (protobuf bytes JSON encoding).
 */
function renderFilePartEl(raw, url, filename, mediaType, taskId = null) {
  const wrap = el('div', 'mt-2 rounded-lg border border-slate-200 bg-white overflow-hidden');

  // Inline image preview for image/* parts. Also registers the image in the
  // fullscreen viewer's navigable list (with the originating taskId, when
  // known, so the fullscreen overlay can surface that turn's
  // prompt-suggestion pills) and wires a click-to-open handler.
  if ((mediaType || '').startsWith('image/') && (raw !== undefined || url)) {
    const imgWrap = el('div', 'bg-slate-50 flex items-center justify-center');
    const img = el('img', 'max-w-full max-h-96 object-contain cursor-zoom-in');
    img.src = url || `data:${mediaType};base64,${raw}`;
    img.alt = filename || 'image';
    const idx = trackImagePart({ raw, url, filename, mediaType, taskId });
    if (idx >= 0) {
      img.addEventListener('click', () => openFullscreen(idx));
    }
    imgWrap.appendChild(img);
    wrap.appendChild(imgWrap);
  }

  const inner = el('div', 'flex items-center gap-3 px-3 py-2.5');

  const iconBox = el('div',
    'w-8 h-8 rounded-md bg-slate-100 flex items-center justify-center ' +
    'text-base shrink-0 select-none');
  iconBox.textContent = fileIcon(mediaType || '');
  inner.appendChild(iconBox);

  const info = el('div', 'flex-1 min-w-0');
  const nameEl = el('div', 'text-sm font-medium text-slate-700 truncate');
  nameEl.textContent = filename || 'file';
  info.appendChild(nameEl);
  if (mediaType) {
    const typeEl = el('div', 'text-[11px] text-slate-400 mt-0.5 font-mono');
    typeEl.textContent = mediaType;
    info.appendChild(typeEl);
  }
  inner.appendChild(info);

  if (raw !== undefined || url) {
    const btn = el('button',
      'text-xs font-medium text-blue-600 hover:text-blue-800 transition ' +
      'shrink-0 px-2.5 py-1 rounded-md hover:bg-blue-50');
    btn.textContent = 'Download';
    btn.addEventListener('click', () => {
      if (raw !== undefined) {
        const blob = b64ToBlob(raw, mediaType || 'application/octet-stream');
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = objUrl;
        a.download = filename || 'download';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(objUrl), 10000);
      } else {
        window.open(url, '_blank');
      }
    });
    inner.appendChild(btn);
  }

  wrap.appendChild(inner);
  return wrap;
}

function fileIcon(mediaType) {
  if (mediaType.startsWith('image/'))   return '🖼';
  if (mediaType.startsWith('audio/'))   return '🎵';
  if (mediaType.startsWith('video/'))   return '🎬';
  if (mediaType === 'application/pdf')  return '📋';
  if (mediaType.includes('spreadsheet') || mediaType === 'application/vnd.ms-excel'
      || mediaType === 'text/csv' || mediaType === 'application/csv'
      || mediaType === 'text/tab-separated-values') return '📊';
  if (mediaType.includes('json') || mediaType.includes('xml')) return '{}';
  if (mediaType.includes('zip') || mediaType.includes('archive')) return '🗜';
  if (mediaType.startsWith('text/'))    return '📄';
  return '📎';
}

/**
 * Build a thumbnail tile for an attached file.
 *
 * For ``image/*`` parts, we keep the original behaviour: an ``<img>``
 * with the file's URL (or base64 data URL for transcript-restored
 * uploads). Anything else (.csv, .xlsx, .pdf, …) would render as a
 * broken-image icon if we tried to point ``<img src>`` at it, so we
 * substitute a generic file tile — the type emoji from ``fileIcon``
 * stacked above a truncated filename.
 *
 * Callers control the tile's footprint via ``sizeClass`` (matches the
 * surrounding flex layout) and decide whether a fullscreen click
 * handler should be wired for images. ``showName`` adds the filename
 * label under non-image icons; it's off for the small preview strip
 * (tooltip-only) and on for the user bubble (the message is now sent
 * and the filename is the only durable identifier).
 */
function attachmentTile(file, { sizeClass, onImageClick, showName }) {
  const wrap = el('div',
    `relative ${sizeClass} rounded-md overflow-hidden ` +
    'border border-slate-200 bg-slate-50 ' +
    'flex flex-col items-center justify-center group');
  wrap.title = file.name;

  const isImage = (file.mediaType || '').startsWith('image/');
  if (isImage) {
    const img = el('img', 'w-full h-full object-cover' +
      (onImageClick ? ' cursor-zoom-in' : ''));
    img.src = file.url
      || (file.base64 ? `data:${file.mediaType};base64,${file.base64}` : '');
    img.alt = file.name;
    if (onImageClick) img.addEventListener('click', onImageClick);
    wrap.appendChild(img);
    return wrap;
  }

  const icon = el('div', 'text-2xl select-none leading-none');
  icon.textContent = fileIcon(file.mediaType || '');
  wrap.appendChild(icon);
  if (showName) {
    const name = el('div',
      'text-[10px] leading-tight text-slate-600 mt-1 px-1 text-center ' +
      'truncate w-full');
    name.textContent = file.name;
    wrap.appendChild(name);
  }
  return wrap;
}

function b64ToBlob(b64, mimeType) {
  const byteChars = atob(b64);
  const byteArrays = [];
  for (let offset = 0; offset < byteChars.length; offset += 512) {
    const slice = byteChars.slice(offset, offset + 512);
    const bytes = new Uint8Array(slice.length);
    for (let i = 0; i < slice.length; i++) bytes[i] = slice.charCodeAt(i);
    byteArrays.push(bytes);
  }
  return new Blob(byteArrays, { type: mimeType });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

// Parts within one item are concatenated directly so token-level spacing is preserved.
// raw=true skips the final trim — needed for the streaming accumulation path.
function collectTexts(items, raw = false) {
  const texts = [];
  for (const item of items) {
    const combined = (item.parts || [])
      .map(p => (typeof p.text === 'string' ? p.text : ''))
      .join('');
    const text = raw ? combined : combined.trim();
    if (text) texts.push(text);
  }
  return texts;
}

function isTransientAgentText(text) {
  const n = text.trim().toLowerCase();
  return n === 'processing request...' || n === 'building response parts…';
}

function uniqueTexts(texts) {
  return [...new Set(texts)];
}

function genUUID() {
  return crypto.randomUUID
    ? crypto.randomUUID()
    : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
      });
}

function persist(id) { localStorage.setItem(CID_STORAGE_KEY, id); }
function refreshCid() { $cid.textContent = contextId; }
// Single source of truth for "is a turn in flight?". Disables every input
// surface (main composer + fullscreen composer) so the user can't queue a
// new prompt while one is still running, regardless of where they sent
// from or whether fullscreen is currently visible.
function setBusy(v)   {
  busy = v;
  $sendBtn.disabled = v;
  $input.disabled = v;
  $streamToggle.disabled = v;
  $attachBtn.disabled = v;
  $fsSend.disabled = v;
  $fsInput.disabled = v;
  for (const b of $fsSuggestions.querySelectorAll('button')) b.disabled = v;
  $stopBtn.classList.toggle('hidden', !v || !streamingEnabled);
  // Keep the fullscreen spinner in sync with global busy state so chat-
  // initiated turns also show a loader when the viewer is open. The
  // fullscreen-initiated path skips the spinner once its result image is
  // in place to avoid hiding the just-rendered image behind a spinner.
  if (fullscreenOpen) {
    setFullscreenLoading(v && !(fullscreenBusy && fullscreenTurnHadImage));
  }
}
function scrollEnd()  { $msgs.scrollTop = $msgs.scrollHeight; }

function el(tag, cls = '') {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

function append(cls, html) {
  const d = el('div', cls);
  d.innerHTML = html;
  $msgs.appendChild(d);
  scrollEnd();
}

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
