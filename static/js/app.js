// ── Config ─────────────────────────────────────────────────────────────────
window.PBCFG = null;

// Map config theme keys → CSS custom property names used in style.css
const _CSS_VAR_MAP = {
    primary:        '--primary',
    primary_hover:  '--primary-h',
    danger:         '--danger',
    background:     '--bg',
    surface:        '--surface',
    border:         '--border',
    text_primary:   '--text',
    text_secondary: '--text-sub',
    text_muted:     '--text-muted',
    code_bg:        '--code-bg',
    navbar_bg:      '--nav-bg',
    navbar_border:  '--nav-border',
};

// Map config ui keys → CSS custom property names
const _UI_VAR_MAP = {
    border_radius: '--radius',
};

// Fetch config and initialise application when ready
document.addEventListener('DOMContentLoaded', () => {
    // 1. Initialise DOM-dependent listeners IMMEDIATELY
    initAutoSave();
    
    const toggleBtn = document.getElementById('themeToggle');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', toggleTheme);
    }

    // 2. Fetch config and apply theme/UI tweaks as a background enhancement
    (async function loadConfig() {
        try {
            const resp = await fetch('/api/config');
            if (resp.ok) {
                window.PBCFG = await resp.json();
                // Re-apply in case config changed defaults
                initialiseTheme();
                applyUiVars();
                // refresh autosave settings if they come from config
                if (window.PBCFG?.features?.auto_save_draft === false) {
                     // If now disabled, we could stop listeners, but for simplicity
                     // we just won't save next time.
                }
            }
        } catch (e) {
            console.warn('Could not load /api/config, using CSS fallbacks.', e);
        }
    })();

    // Initial pass with CSS fallbacks
    initialiseTheme();
    applyUiVars();
});

// ── Theme Management ────────────────────────────────────────────────────────

function initialiseTheme() {
    const saved         = localStorage.getItem('theme');
    const configDefault = window.PBCFG?.theme?.default || 'auto';
    let theme;

    if (saved === 'light' || saved === 'dark') {
        theme = saved;
    } else if (configDefault === 'dark') {
        theme = 'dark';
    } else if (configDefault === 'light') {
        theme = 'light';
    } else {
        // 'auto' or unset → follow OS preference
        theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    applyTheme(theme);

    // Live OS theme changes (only when user hasn't pinned a preference)
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
        if (!localStorage.getItem('theme')) {
            applyTheme(e.matches ? 'dark' : 'light');
        }
    });
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    applyConfigCssVars(theme);
    swapPrismTheme(theme);
    updateThemeToggle(theme);
}

function applyConfigCssVars(theme) {
    const cfg = window.PBCFG?.theme;
    if (!cfg) return;
    const palette = cfg[theme] || {};
    const root    = document.documentElement;
    for (const [key, cssVar] of Object.entries(_CSS_VAR_MAP)) {
        if (palette[key] !== undefined) {
            root.style.setProperty(cssVar, palette[key]);
        }
    }
}

function applyUiVars() {
    const ui = window.PBCFG?.ui;
    if (!ui) return;
    const root = document.documentElement;
    for (const [key, cssVar] of Object.entries(_UI_VAR_MAP)) {
        if (ui[key] !== undefined) {
            root.style.setProperty(cssVar, ui[key]);
        }
    }
}

function swapPrismTheme(theme) {
    const light = document.getElementById('prism-light');
    const dark  = document.getElementById('prism-dark');
    if (!light || !dark) return;
    if (theme === 'dark') {
        light.disabled = true;
        dark.disabled  = false;
    } else {
        light.disabled = false;
        dark.disabled  = true;
    }
}

window.toggleTheme = function () {
    const current  = document.documentElement.getAttribute('data-theme') || 'light';
    const newTheme = current === 'light' ? 'dark' : 'light';
    applyTheme(newTheme);
    if (window.PBCFG?.theme?.allow_user_toggle !== false) {
        localStorage.setItem('theme', newTheme);
    }
}

function updateThemeToggle(theme) {
    const btn = document.querySelector('.theme-toggle');
    if (!btn) return;
    btn.textContent = theme === 'light' ? '🌙' : '☀️';
    btn.title = `Switch to ${theme === 'light' ? 'dark' : 'light'} mode`;
}

// ── Clipboard ───────────────────────────────────────────────────────────────

async function copyToClipboard(text) {
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (e) { /* fall through */ }
    // Fallback: temporary textarea
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) {}
    document.body.removeChild(ta);
    return ok;
}

// ── Time formatting ─────────────────────────────────────────────────────────

window.formatDateTime = function (dateString) {
    const date      = new Date(dateString);
    const now       = new Date();
    const diffMs    = now - date;
    const diffMins  = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays  = Math.floor(diffMs / 86400000);

    if (diffMins  < 1)   return 'Just now';
    if (diffMins  < 60)  return `${diffMins} minute${diffMins  > 1 ? 's' : ''} ago`;
    if (diffHours < 24)  return `${diffHours} hour${diffHours  > 1 ? 's' : ''} ago`;
    if (diffDays  < 7)   return `${diffDays} day${diffDays     > 1 ? 's' : ''} ago`;
    if (diffDays  < 30)  { const w = Math.floor(diffDays / 7);  return `${w} week${w  > 1 ? 's' : ''} ago`; }
    if (diffDays  < 365) { const m = Math.floor(diffDays / 30); return `${m} month${m > 1 ? 's' : ''} ago`; }
    const y = Math.floor(diffDays / 365);
    return `${y} year${y > 1 ? 's' : ''} ago`;
};

// ── Draft auto-save ─────────────────────────────────────────────────────────

function initAutoSave() {
    const contentTextarea = document.getElementById('content');
    const titleInput      = document.getElementById('title');
    if (!contentTextarea) return;

    if (window.PBCFG?.features?.auto_save_draft !== false) {
        loadDraft();
    }

    let saveTimeout;
    function saveDraft() {
        clearTimeout(saveTimeout);
        saveTimeout = setTimeout(() => {
            const draft = {
                title:     titleInput ? titleInput.value : '',
                content:   contentTextarea.value,
                timestamp: new Date().toISOString()
            };
            if (draft.content.trim()) {
                localStorage.setItem('paste_draft', JSON.stringify(draft));
            } else {
                localStorage.removeItem('paste_draft');
            }
        }, 1000);
    }

    contentTextarea.addEventListener('input', saveDraft);
    if (titleInput) titleInput.addEventListener('input', saveDraft);
}

function loadDraft() {
    const raw = localStorage.getItem('paste_draft');
    if (!raw) return;
    try {
        const draft    = JSON.parse(raw);
        const maxDays  = window.PBCFG?.features?.draft_max_age_days ?? 7;
        const maxAge   = maxDays * 86400000;
        const draftAge = Date.now() - new Date(draft.timestamp).getTime();

        if (draftAge > maxAge) { localStorage.removeItem('paste_draft'); return; }

        if (draft.content && draft.content.trim() &&
            confirm('A draft was found. Would you like to restore it?')) {
            const title   = document.getElementById('title');
            const content = document.getElementById('content');
            if (title)   title.value   = draft.title   || '';
            if (content) content.value = draft.content || '';
        }
    } catch (e) {
        localStorage.removeItem('paste_draft');
    }
}

window.clearDraft = function () {
    console.log('[Draft] Clearing localStorage draft...');
    localStorage.removeItem('paste_draft');
}

// ── Keyboard shortcuts ──────────────────────────────────────────────────────

document.addEventListener('keydown', function (e) {
    if (window.PBCFG?.features?.keyboard_shortcuts === false) return;

    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        const textarea = document.getElementById('content');
        if (textarea) { e.preventDefault(); textarea.focus(); }
    }
});

// Removed redundant DOMContentLoaded at the bottom — handled by the single listener at the top.

// Confirmation prompts: <form data-confirm="Are you sure?">. (Inline onsubmit handlers are blocked
// by the Content-Security-Policy, so forms ask for confirmation through this attribute instead.)
document.addEventListener('submit', function (e) {
    var message = e.target && e.target.dataset ? e.target.dataset.confirm : '';
    if (message && !window.confirm(message)) e.preventDefault();
});
