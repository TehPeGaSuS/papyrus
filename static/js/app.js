// ── Config ─────────────────────────────────────────────────────────────────
window.PBCFG = null;


// Map config ui keys → CSS custom property names
const _UI_VAR_MAP = {
    border_radius: '--radius',
};

// Fetch config and initialise application when ready
document.addEventListener('DOMContentLoaded', () => {
    // Older versions kept an unsent draft in localStorage; remove any that is still around.
    try { localStorage.removeItem('paste_draft'); } catch (e) { /* storage unavailable */ }

    // 1. Initialise DOM-dependent listeners IMMEDIATELY
    initPalette();

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
    swapPrismTheme(theme);
    updateThemeToggle(theme);
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
    const label = `Switch to ${theme === 'light' ? 'dark' : 'light'} mode`;   // the icon itself is CSS
    btn.title = label;
    btn.setAttribute('aria-label', label);
}

// Colour theme dropdown. The head script has already applied a saved choice, before first paint.
function initPalette() {
    const select = document.getElementById('paletteSelect');
    if (!select) return;
    select.value = document.documentElement.getAttribute('data-palette');
    select.addEventListener('change', () => {
        document.documentElement.setAttribute('data-palette', select.value);
        try { localStorage.setItem('palette', select.value); } catch (e) { /* private mode: lasts for this page */ }
    });
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
