'use strict';
/**
 * paste_create.js: logic for the paste creation page.
 * Depends on: nothing but the DOM (app.js only supplies the theme toggle).
 *
 * Besides saving the paste it gives the plain textarea the editor basics from Gist-style
 * editors: indent mode (spaces or tabs), indent size (2, 4, 8), soft or no wrap, Tab/Shift+Tab
 * indenting, and keeping the indentation of the previous line on Enter.
 */

document.addEventListener('DOMContentLoaded', function () {
    const $ = id => document.getElementById(id);
    const textarea    = $('content');
    const submitBtn   = $('submitBtn');
    const langSelect  = $('language');
    const filename    = $('filename');
    const indentMode  = $('indentMode');
    const indentSize  = $('indentSize');
    const wrapMode    = $('wrapMode');

    const store = {
        get(key)       { try { return localStorage.getItem(key); } catch (e) { return null; } },
        set(key, value){ try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } },
    };

    // ── Language: "Auto" follows the filename's extension ────────────────────
    let hints = { extensions: {}, names: {} };
    try { hints = JSON.parse($('languageHints').textContent); } catch (e) { /* no auto label */ }

    function detectLanguage(name) {
        name = (name || '').trim().toLowerCase();
        if (!name) return null;
        const dot = name.lastIndexOf('.');
        return hints.names[name] || (dot >= 0 ? hints.extensions[name.slice(dot + 1)] : null) || null;
    }

    function autoOption() { return langSelect.querySelector('option[value="auto"]'); }
    function updateAutoLabel() {
        const option = autoOption();
        if (!option) return;
        const detected = detectLanguage(filename.value);
        option.textContent = detected ? 'Auto (' + detected + ')' : 'Auto-detect';
    }

    fetch('/api/languages')
        .then(r => r.json())
        .then(langs => {
            langSelect.innerHTML = '';
            const auto = document.createElement('option');
            auto.value = 'auto';
            langSelect.appendChild(auto);
            langs.forEach(l => {
                const o = document.createElement('option');
                o.value = l.value;
                o.textContent = l.name;
                langSelect.appendChild(o);
            });
            updateAutoLabel();
        })
        .catch(() => {});
    filename.addEventListener('input', updateAutoLabel);

    // ── Expiry and discussion preferences ────────────────────────────────────
    const expirySelect = $('expires_in');
    const savedExpiry  = store.get('preferred_expiry');
    if (savedExpiry) expirySelect.value = savedExpiry;
    expirySelect.addEventListener('change', () => store.set('preferred_expiry', expirySelect.value));

    const discussCheck = $('allowDiscussions');
    if (discussCheck) {
        discussCheck.checked = store.get('preferred_discussions') === 'true';
        discussCheck.addEventListener('change', () => store.set('preferred_discussions', discussCheck.checked));
    }

    // ── Editor options: indent mode / size, line wrap ────────────────────────
    function restore(select, key, fallback) {
        const saved = store.get(key);
        select.value = Array.from(select.options).some(o => o.value === saved) ? saved : fallback;
    }
    restore(indentMode, 'indent_mode', 'spaces');
    restore(indentSize, 'indent_size', '4');
    restore(wrapMode,   'wrap_mode',   'soft');

    function applyEditorOptions() {
        textarea.style.tabSize = indentSize.value;                 // how a tab character is drawn
        const off = wrapMode.value === 'off';
        textarea.wrap = off ? 'off' : 'soft';
        textarea.classList.toggle('no-wrap', off);
    }
    [[indentMode, 'indent_mode'], [indentSize, 'indent_size'], [wrapMode, 'wrap_mode']].forEach(([select, key]) =>
        select.addEventListener('change', () => { store.set(key, select.value); applyEditorOptions(); textarea.focus(); }));
    applyEditorOptions();

    const unit = () => indentMode.value === 'tabs' ? '\t' : ' '.repeat(parseInt(indentSize.value, 10));

    // Edits go through execCommand so the browser's undo (Ctrl+Z) keeps working.
    function replaceRange(start, end, text) {
        textarea.focus();
        textarea.setSelectionRange(start, end);
        if (!document.execCommand || !document.execCommand('insertText', false, text)) {
            textarea.setRangeText(text, start, end, 'end');
            textarea.dispatchEvent(new Event('input', { bubbles: true }));
        }
    }
    const lineStartOf = pos => textarea.value.lastIndexOf('\n', pos - 1) + 1;
    function lineEndOf(pos) {
        const i = textarea.value.indexOf('\n', pos);
        return i === -1 ? textarea.value.length : i;
    }

    function indent() {
        const { selectionStart: start, selectionEnd: end, value } = textarea;
        const multiline = value.slice(start, end).includes('\n');
        if (!multiline) {
            // A caret (or a selection inside one line): insert one indent, up to the next tab stop.
            let text = unit();
            if (indentMode.value !== 'tabs') {
                const size = parseInt(indentSize.value, 10);
                text = ' '.repeat(size - ((start - lineStartOf(start)) % size));
            }
            replaceRange(start, end, text);
            return;
        }
        const from = lineStartOf(start);
        const to = lineEndOf(end > start && value[end - 1] === '\n' ? end - 1 : end);
        const lines = value.slice(from, to).split('\n');
        const block = lines.map(l => (l.trim() ? unit() + l : l)).join('\n');
        replaceRange(from, to, block);
        const added = block.length - (to - from);
        textarea.setSelectionRange(start + (lines[0].trim() ? unit().length : 0), end + added);
    }

    function outdent() {
        const { selectionStart: start, selectionEnd: end, value } = textarea;
        const from = lineStartOf(start);
        const to = lineEndOf(end > start && value[end - 1] === '\n' ? end - 1 : end);
        const size = parseInt(indentSize.value, 10);
        let removedFirst = 0, removedTotal = 0;
        const lines = value.slice(from, to).split('\n').map((l, i) => {
            const m = /^(\t| {1,8})/.exec(l);
            const n = !m ? 0 : m[0] === '\t' ? 1 : Math.min(m[0].length, size);
            if (i === 0) removedFirst = n;
            removedTotal += n;
            return l.slice(n);
        });
        if (!removedTotal) return;
        replaceRange(from, to, lines.join('\n'));
        textarea.setSelectionRange(Math.max(from, start - removedFirst), Math.max(from, end - removedTotal));
    }

    function autoIndent(e) {
        const { selectionStart: start, selectionEnd: end, value } = textarea;
        const lineStart = lineStartOf(start);
        const lead = /^[ \t]*/.exec(value.slice(lineStart, start))[0];    // only what is left of the caret
        const opener = '{[('.includes(value[start - 1] || ' ') ? unit() : '';
        if (!lead && !opener) return;                                      // nothing to carry over
        e.preventDefault();
        replaceRange(start, end, '\n' + lead + opener);
    }

    // Esc, then Tab, moves focus on: otherwise the editor would trap keyboard users.
    let releaseTab = false;
    textarea.addEventListener('focus', () => { releaseTab = false; });
    textarea.addEventListener('keydown', e => {
        if (e.key === 'Escape') { releaseTab = true; return; }
        const plain = !e.ctrlKey && !e.altKey && !e.metaKey;
        if (e.key === 'Tab' && plain && !releaseTab) {
            e.preventDefault();
            if (e.shiftKey) outdent(); else indent();
            return;
        }
        releaseTab = false;
        if (e.key === 'Enter' && plain && !e.shiftKey && !e.isComposing) autoIndent(e);
    });

    // ── Ctrl/Cmd+S shortcut ──────────────────────────────────────────────────
    document.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            submitPaste();
        }
    });
    submitBtn.addEventListener('click', submitPaste);

    // ── Clear ────────────────────────────────────────────────────────────────
    const clearBtn = $('clearBtn');
    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            textarea.value = '';
            $('title').value = '';
            filename.value = '';
            updateAutoLabel();
            textarea.focus();
        });
    }

    // ── Submit ───────────────────────────────────────────────────────────────
    async function submitPaste() {
        const content = textarea.value;
        if (!content.trim()) { textarea.focus(); return; }

        submitBtn.disabled    = true;
        submitBtn.textContent = '…';
        try {
            const resp = await fetch('/create', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({
                    title:       $('title').value.trim(),       // empty: the server uses the filename
                    filename:    filename.value.trim(),
                    content,
                    language:    langSelect.value || 'auto',     // "auto": detected from the filename
                    expires_in:  expirySelect.value,
                    discussions: discussCheck ? discussCheck.checked : false,
                }),
            });
            const result = await resp.json();
            if (result.error) {
                alert('Error: ' + result.error);
                submitBtn.disabled    = false;
                submitBtn.textContent = 'Save';
                return;
            }
            if (result.deletion_token) store.set('del_' + result.paste_id, result.deletion_token);
            window.location.href = result.url;
        } catch (err) {
            console.error(err);
            alert('Failed to create paste. Try again.');
            submitBtn.disabled    = false;
            submitBtn.textContent = 'Save';
        }
    }
});
