'use strict';
/**
 * paste_create.js — Logic for the paste creation page.
 * Depends on: app.js (clearDraft)
 */

document.addEventListener('DOMContentLoaded', function () {
    const textarea   = document.getElementById('content');
    const submitBtn  = document.getElementById('submitBtn');
    const langSelect = document.getElementById('language');

    // ── Load languages from API ──────────────────────────────────────────────
    fetch('/api/languages')
        .then(r => r.json())
        .then(langs => {
            langSelect.innerHTML = '';
            langs.forEach(l => {
                const o = document.createElement('option');
                o.value = l.value;
                o.textContent = l.name;
                langSelect.appendChild(o);
            });
            // Restore last-used language preference
            const saved = localStorage.getItem('preferred_language');
            if (saved) langSelect.value = saved;
        })
        .catch(() => {});

    langSelect.addEventListener('change', () =>
        localStorage.setItem('preferred_language', langSelect.value));

    // ── Restore expiry preference ────────────────────────────────────────────
    const expirySelect = document.getElementById('expires_in');
    const savedExpiry  = localStorage.getItem('preferred_expiry');
    if (savedExpiry) expirySelect.value = savedExpiry;
    expirySelect.addEventListener('change', () =>
        localStorage.setItem('preferred_expiry', expirySelect.value));

    // ── Restore discussions preference ───────────────────────────────────────
    const discussCheck = document.getElementById('allowDiscussions');
    if (discussCheck) {
        discussCheck.checked = localStorage.getItem('preferred_discussions') === 'true';
        discussCheck.addEventListener('change', () =>
            localStorage.setItem('preferred_discussions', discussCheck.checked));
    }

    // ── Ctrl/Cmd+S shortcut ──────────────────────────────────────────────────
    document.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            submitPaste();
        }
    });

    submitBtn.addEventListener('click', submitPaste);

    // ── Clear Draft ──────────────────────────────────────────────────────────
    const clearBtn = document.getElementById('clearBtn');
    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            console.log('[Clear] Manual clear requested.');
            
            // Clear the editor and title
            textarea.value = '';
            const title = document.getElementById('title');
            if (title) title.value = '';
            
            // Wipe the localStorage draft via the global helper in app.js
            if (typeof window.clearDraft === 'function') {
                window.clearDraft();
            } else {
                localStorage.removeItem('paste_draft');
            }
            
            textarea.focus();
            console.log('[Clear] Draft wiped.');
        });
    }

    // ── Submit ───────────────────────────────────────────────────────────────

    async function submitPaste() {
        const content    = textarea.value;
        const title      = document.getElementById('title').value || 'Untitled';
        const language   = langSelect.value;
        const expires_in = expirySelect.value;

        if (!content.trim()) { textarea.focus(); return; }

        const discussions = document.getElementById('allowDiscussions')?.checked ?? false;

        submitBtn.disabled    = true;
        submitBtn.textContent = '…';

        try {
            const postBody = { title, content, language, expires_in, discussions };

            const resp   = await fetch('/create', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify(postBody),
            });
            const result = await resp.json();

            if (result.error) {
                alert('Error: ' + result.error);
                submitBtn.disabled    = false;
                submitBtn.textContent = 'Save';
            } else {
                if (typeof window.clearDraft === 'function') {
                    window.clearDraft();
                }
                if (result.deletion_token) {
                    localStorage.setItem('del_' + result.paste_id, result.deletion_token);
                }
                window.location.href = result.url;
            }
        } catch (err) {
            console.error(err);
            alert('Failed to create paste. Try again.');
            submitBtn.disabled    = false;
            submitBtn.textContent = 'Save';
        }
    }
});
