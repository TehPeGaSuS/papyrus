'use strict';
/**
 * paste_view.js — behaviour for the server-rendered paste view page.
 * Depends on: app.js (copyToClipboard), Prism.
 * A paste is one or more .file-card sections; each has its own Raw / Copy / Download / Source|Preview controls.
 */

document.addEventListener('DOMContentLoaded', () => {
    if (!document.getElementById('viewFull')) return; // Not a paste view page
    initPasteActions();
    initLineHighlight(); // register Prism hook before initLineNumbers triggers Prism
    initLineNumbers();
    initWordWrap();
    initComments();
    tidyAddressBar();
    initRemovalLink();
    initMarkdownPreview();
});

// ?created=1 and ?reported=1 only tell the server to show a one-time notice. Once the page has it, take them
// out of the address bar, so the URL people copy and share is just /<id> (a refresh shows no notice again).
function tidyAddressBar() {
    const url = new URL(window.location.href);
    const had = ['created', 'reported'].filter(name => url.searchParams.has(name));
    if (!had.length) return;
    had.forEach(name => url.searchParams.delete(name));
    history.replaceState(null, '', url.pathname + url.search + url.hash);
}

function pasteMeta() {
    return document.getElementById('viewFull').dataset;
}

function fileCards() {
    return Array.from(document.querySelectorAll('.file-card'));
}

async function fetchRawContent(card) {
    const resp = await fetch(card.dataset.raw);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.text();
}

// The buttons sit in the top bar (one file) or in each card's header (several); data-file says which card.
function cardOf(button) {
    return document.querySelector('.file-card[data-file="' + button.dataset.file + '"]');
}

function initPasteActions() {
    document.querySelectorAll('[data-role="copy"]').forEach(btn => btn.addEventListener('click', () => copyFile(btn)));
    document.querySelectorAll('[data-role="download"]').forEach(btn => btn.addEventListener('click', () => downloadFile(btn)));
}

function initLineNumbers() {
    const toggle = document.getElementById('lineNoToggle');
    if (!toggle) return;

    // Load preference from localStorage (default to checked)
    const stored = localStorage.getItem('show_line_numbers');
    toggle.checked = (stored === null) ? true : (stored === 'true');

    const updateLines = () => {
        fileCards().forEach(card => {
            const pre = card.querySelector('.file-pre');
            pre.classList.toggle('line-numbers', toggle.checked);
            if (!toggle.checked) pre.querySelector('.line-numbers-rows')?.remove();
            // Always re-highlight so Prism's line-numbers plugin runs (works for plain text too)
            Prism.highlightElement(card.querySelector('.file-code'));
        });
        localStorage.setItem('show_line_numbers', toggle.checked);
    };

    toggle.addEventListener('change', updateLines);
    updateLines();   // initial state: renders line numbers without needing a toggle
}

async function copyFile(btn) {
    let ok = false;
    try { ok = await copyToClipboard(await fetchRawContent(cardOf(btn))); } catch (e) { /* reported below */ }
    const t = btn.textContent;
    btn.textContent = ok ? 'Copied!' : 'Failed';
    setTimeout(() => btn.textContent = t, 1500);
}

async function downloadFile(btn) {
    const card = cardOf(btn);
    const { title = 'untitled' } = pasteMeta();
    const { language = 'text', filename = '' } = card.dataset;
    const extMap = {
        javascript: '.js',  typescript: '.ts',  python:  '.py',   java:       '.java',
        c:          '.c',   cpp:        '.cpp',  csharp:  '.cs',   html:       '.html',
        css:        '.css', scss:       '.scss', sql:     '.sql',  json:       '.json',
        yaml:       '.yaml',xml:        '.xml',  bash:    '.sh',   powershell: '.ps1',
        php:        '.php', ruby:       '.rb',   go:      '.go',   rust:       '.rs',
        swift:      '.swift',kotlin:    '.kt',   markdown:'.md',   diff:       '.diff',
        docker:     '',     nginx:      '.conf', toml:    '.toml', ini:        '.ini',
    };
    let content;
    try { content = await fetchRawContent(card); } catch (e) { alert('Download failed.'); return; }
    const safe = s => s.replace(/[^a-z0-9._\-]/gi, '_');
    const downloadName = filename ? safe(filename) : safe(title) + (extMap[language] ?? '.txt');
    const blob = new Blob([content], { type: 'text/plain' });
    const url  = URL.createObjectURL(blob);
    const a    = Object.assign(document.createElement('a'), { href: url, download: downloadName });
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

function initRemovalLink() {
    const copy = document.getElementById('copyRemoval');
    const field = document.getElementById('removalUrl');
    if (!copy || !field) return;
    field.addEventListener('focus', () => field.select());
    copy.addEventListener('click', async () => {
        const ok = await copyToClipboard(field.value);
        const label = copy.textContent;
        copy.textContent = ok ? 'Copied!' : 'Press Ctrl+C';
        if (!ok) field.select();
        setTimeout(() => { copy.textContent = label; }, 1500);
    });
}

// ── Line highlight / linking ──────────────────────────────────────────────────
// ?L=5 or ?L=5-12 marks lines of one file; ?f=2 says which one (the first when absent).

let _hlFile    = null; // the card whose lines are highlighted
let _hlAnchor  = null; // anchor line for shift-click range
let _hlStart   = null;
let _hlEnd     = null;
let _hlScrolled = false;

function initLineHighlight() {
    const params = new URLSearchParams(window.location.search);
    const m = (params.get('L') || '').match(/^(\d+)(?:-(\d+))?$/);
    if (m) {
        _hlFile   = fileCards()[Math.max(0, parseInt(pasteMeta().highlightFile, 10) - 1)] || null;
        _hlStart  = parseInt(m[1], 10);
        _hlEnd    = m[2] ? parseInt(m[2], 10) : _hlStart;
        _hlAnchor = _hlStart;
    }

    // After every Prism highlight, wire gutter clicks and redraw the highlight bars of that card.
    // initLineNumbers() calls Prism, which fires this hook.
    Prism.hooks.add('complete', function (env) {
        const card = env.element && env.element.closest ? env.element.closest('.file-card') : null;
        if (!card || !env.element.classList.contains('file-code')) return;
        _wireLineClicks(card);
        if (_hlStart !== null && card === _hlFile) {
            _drawHighlight(_hlStart, _hlEnd);
            if (!_hlScrolled) {
                _hlScrolled = true;
                card.querySelector('.line-hl')?.scrollIntoView({ block: 'center' });
            }
        }
    });
}

function _wireLineClicks(card) {
    card.querySelectorAll('.line-numbers-rows > span').forEach((span, i) => {
        if (span.dataset.lineWired) return;
        span.dataset.lineWired = '1';
        const lineNum = i + 1;
        span.title = `Line ${lineNum}`;
        span.addEventListener('click', (e) => {
            if (_hlFile !== card) { _clearHighlight(); _hlFile = card; _hlAnchor = null; _hlStart = null; _hlEnd = null; }
            if (e.shiftKey && _hlAnchor !== null) {
                const a = Math.min(_hlAnchor, lineNum);
                const b = Math.max(_hlAnchor, lineNum);
                _hlStart = a; _hlEnd = b;
                _drawHighlight(a, b);
                _updateLineUrl(a, b);
            } else if (_hlStart === lineNum && _hlEnd === lineNum) {
                // Clicking the sole highlighted line → clear
                _hlAnchor = null; _hlStart = null; _hlEnd = null;
                _clearHighlight();
                _clearLineUrl();
            } else {
                _hlAnchor = lineNum;
                _hlStart  = lineNum;
                _hlEnd    = lineNum;
                _drawHighlight(lineNum, lineNum);
                _updateLineUrl(lineNum, lineNum);
            }
        });
    });
}

function _clearHighlight() {
    document.querySelectorAll('.file-pre .line-hl').forEach(el => el.remove());
    document.querySelectorAll('.line-numbers-rows > span.hl-active').forEach(s => s.classList.remove('hl-active'));
}

function _clearLineUrl() {
    const url = new URL(window.location.href);
    url.searchParams.delete('L');
    url.searchParams.delete('f');
    history.replaceState(null, '', url.toString());
}

function _drawHighlight(start, end) {
    const card = _hlFile;
    const viewPre = card && card.querySelector('.file-pre');
    const code    = card && card.querySelector('.file-code');
    if (!viewPre || !code) return;

    // Remove old bars and gutter classes
    _clearHighlight();
    viewPre.querySelectorAll('.line-numbers-rows > span').forEach(
        (s, i) => s.classList.toggle('hl-active', i + 1 >= start && i + 1 <= end)
    );

    const lineH  = parseFloat(getComputedStyle(code).lineHeight) || 22.4;
    const padTop = parseFloat(getComputedStyle(viewPre).paddingTop) || 16;

    const frag = document.createDocumentFragment();
    for (let i = start; i <= end; i++) {
        const bar = document.createElement('div');
        bar.className = 'line-hl';
        bar.style.top    = (padTop + (i - 1) * lineH) + 'px';
        bar.style.height = lineH + 'px';
        frag.appendChild(bar);
    }
    // Insert before <code> so bars sit below code text in z-order
    viewPre.insertBefore(frag, viewPre.firstChild);
}

function _updateLineUrl(start, end) {
    const url = new URL(window.location.href);
    url.searchParams.set('L', start === end ? String(start) : `${start}-${end}`);
    const index = fileCards().indexOf(_hlFile);
    if (index > 0) url.searchParams.set('f', String(index + 1)); else url.searchParams.delete('f');
    history.replaceState(null, '', url.toString());
}

// ── Word wrap ─────────────────────────────────────────────────────────────────

function initWordWrap() {
    const toggle  = document.getElementById('wrapToggle');
    const viewDiv = document.getElementById('viewFull');   // the wrap-lines class is read by the CSS of every card
    if (!toggle || !viewDiv) return;

    const stored  = localStorage.getItem('wrap_lines');
    const enabled = stored === null ? false : stored === 'true';   // default: no wrap, like GitHub
    toggle.checked = enabled;
    viewDiv.classList.toggle('wrap-lines', enabled);

    toggle.addEventListener('change', () => {
        viewDiv.classList.toggle('wrap-lines', toggle.checked);
        localStorage.setItem('wrap_lines', toggle.checked);
    });
}

function initComments() {
    const panel = document.getElementById('commentsPanel');
    if (!panel) return;

    // Collapse / expand via header click
    const header = document.getElementById('commentsToggle');
    if (header) {
        header.addEventListener('click', () => {
            panel.classList.toggle('collapsed');
            const chevron = document.getElementById('commentsChevron');
            if (chevron) chevron.textContent = panel.classList.contains('collapsed') ? '\u25B2' : '\u25BC';
        });
    }

    // Local times, and syntax highlighting for fenced code in comments
    document.querySelectorAll('.comment-time').forEach(el => {
        const d = new Date(el.getAttribute('datetime'));
        if (!isNaN(d)) el.textContent = d.toLocaleString();
    });
    const list = document.getElementById('commentsList');
    if (list && window.Prism) Prism.highlightAllUnder(list);

    const form  = document.getElementById('commentForm');
    const input = document.getElementById('commentInput');
    const nick  = document.getElementById('commentNick');
    if (!form || !input) return;

    // Remember the nickname
    if (nick) {
        try { nick.value = localStorage.getItem('comment_nick') || ''; } catch (e) {}
        form.addEventListener('submit', () => {
            try { localStorage.setItem('comment_nick', nick.value.trim()); } catch (e) {}
        });
    }

    input.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            form.requestSubmit();
        }
    });

    // Write / Preview tabs (the server renders the preview so it matches what gets posted)
    const preview = document.getElementById('commentPreview');
    const tabs = form.querySelectorAll('.comment-tab');
    tabs.forEach(tab => tab.addEventListener('click', async () => {
        const showPreview = tab.dataset.tab === 'preview';
        tabs.forEach(t => t.classList.toggle('active', t === tab));
        input.style.display   = showPreview ? 'none' : '';
        preview.style.display = showPreview ? 'block' : 'none';
        if (!showPreview) { input.focus(); return; }
        if (!input.value.trim()) { preview.textContent = 'Nothing to preview.'; return; }
        preview.textContent = 'Rendering\u2026';
        try {
            const resp = await fetch('/api/preview', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ text: input.value }),
            });
            const data = await resp.json();
            if (!resp.ok || data.error) throw new Error(data.error || resp.status);
            preview.innerHTML = data.html; // sanitised server-side by nh3
            if (window.Prism) Prism.highlightAllUnder(preview);
        } catch (e) {
            preview.textContent = 'Preview failed: ' + e.message;
        }
    }));

    // Reply: move the form under the comment being answered. A hidden field tells
    // the server which comment it belongs to; Cancel puts the form back at the bottom.
    const home      = document.getElementById('commentFormHome');
    const parentEl  = document.getElementById('commentParent');
    const replyBar  = document.getElementById('replyBar');
    const replyTo   = document.getElementById('replyTo');

    function resetReply() {
        parentEl.value = '';
        replyBar.hidden = true;
        home.appendChild(form);
    }
    document.getElementById('replyCancel')?.addEventListener('click', () => { resetReply(); input.focus(); });

    document.querySelectorAll('.comment-item .comment-reply').forEach(btn => btn.addEventListener('click', () => {
        const item = btn.closest('.comment-item');
        parentEl.value = btn.dataset.id;
        replyTo.textContent = btn.dataset.nick;
        replyBar.hidden = false;
        item.querySelector(':scope > .comment-replies').prepend(form);
        tabs[0]?.click();
        if (panel.classList.contains('collapsed')) header?.click();
        form.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        input.focus();
    }));
}

// ── Markdown files: Source | Preview ──────────────────────────────────────────
// The links work without JavaScript (?view=preview shows every Markdown file rendered); this makes the switch
// instant. With one file the address bar follows the choice, so the URL stays shareable. The rendered HTML came
// sanitised from the server, the same way comments are.
function initMarkdownPreview() {
    const single = fileCards().length === 1;
    fileCards().forEach(card => {
        const article = card.querySelector('.paste-preview');
        if (!article) return;
        const buttons = document.querySelectorAll('.switch-btn[data-file="' + card.dataset.file + '"]');
        const source  = Array.from(buttons).find(b => b.dataset.role === 'source');
        const preview = Array.from(buttons).find(b => b.dataset.role === 'preview');
        let highlighted = false;

        function show(mode) {
            const on = mode === 'preview';
            card.classList.toggle('previewing', on);
            if (single) document.body.classList.toggle('previewing', on);
            source.classList.toggle('active', !on);
            preview.classList.toggle('active', on);
            source.setAttribute('aria-current', String(!on));
            preview.setAttribute('aria-current', String(on));
            if (on && !highlighted && window.Prism) { Prism.highlightAllUnder(article); highlighted = true; }
            if (single) {
                const url = new URL(window.location.href);
                if (on) url.searchParams.set('view', 'preview'); else url.searchParams.delete('view');
                url.searchParams.delete('L');           // line highlights belong to the source view
                history.replaceState(null, '', url.toString());
            }
        }
        source.addEventListener('click',  e => { e.preventDefault(); show('source'); });
        preview.addEventListener('click', e => { e.preventDefault(); show('preview'); });
        if (card.classList.contains('previewing') && window.Prism) { Prism.highlightAllUnder(article); highlighted = true; }
    });
}
