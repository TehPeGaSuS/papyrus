'use strict';
/**
 * admin.js: the "View" window of the admin panel. Fetches a paste's files and comments from
 * /admin/paste/<id>/data and shows them as plain text (textContent only, nothing is ever parsed as HTML).
 */

document.addEventListener('DOMContentLoaded', () => {
    const dialog = document.getElementById('viewDialog');
    if (!dialog) return;
    const body = document.getElementById('viewBody');
    const actions = document.getElementById('viewActions');

    const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    };
    const when = value => (value || '').replace('T', ' ').slice(0, 16);

    function postButton(label, action, fields, confirmText) {
        const form = el('form', 'inline-form');
        form.method = 'post';
        form.action = action;
        if (confirmText) form.dataset.confirm = confirmText;
        Object.entries(fields).forEach(([name, value]) => {
            const input = el('input');
            input.type = 'hidden'; input.name = name; input.value = value;
            form.appendChild(input);
        });
        const button = el('button', 'btn-delete-small', label);
        button.type = 'submit';
        form.appendChild(button);
        return form;
    }

    function render(data, markedComment) {
        document.getElementById('viewTitle').textContent = data.title;
        document.getElementById('viewMeta').textContent = [
            data.id, 'created ' + when(data.created_at), data.expires_at ? 'expires ' + when(data.expires_at) : 'never expires',
            data.views + ' view' + (data.views === 1 ? '' : 's'), data.address ? 'from ' + data.address : '',
        ].filter(Boolean).join(' \u00b7 ');

        body.replaceChildren();
        data.files.forEach(file => {
            const card = el('section', 'view-file');
            card.appendChild(el('div', 'view-file-head', (file.filename || 'untitled') + ' \u00b7 ' + file.language));
            card.appendChild(el('pre', 'view-file-text', file.content));
            body.appendChild(card);
        });
        if (data.comments.length) {
            body.appendChild(el('h3', 'view-comments-title', 'Comments (' + data.comments.length + ')'));
            data.comments.forEach(c => {
                const box = el('div', 'view-comment' + (c.id === markedComment ? ' marked' : ''));
                box.appendChild(el('div', 'view-comment-meta',
                    (c.nick || 'anonymous') + ' \u00b7 ' + when(c.created_at) + (c.creator_ip ? ' \u00b7 ' + c.creator_ip : '')));
                box.appendChild(el('div', 'view-comment-text', c.content));
                body.appendChild(box);
            });
        }

        actions.replaceChildren();
        const open = el('a', 'btn-small', 'Open the page');
        open.href = data.url; open.target = '_blank'; open.rel = 'noopener';
        actions.appendChild(open);
        if (markedComment) {
            actions.appendChild(postButton('Delete comment', '/admin/comments/' + encodeURIComponent(markedComment) + '/delete',
                                           {}, 'Delete this comment? Its replies stay.'));
        }
        actions.appendChild(postButton('Delete paste', '/admin/delete/' + encodeURIComponent(data.id), {},
                                       'Delete this paste and all its comments permanently?'));
        if (data.address) {
            actions.appendChild(postButton('Block author', '/admin/blocks', markedComment ? { comment_id: markedComment } : { paste_id: data.id },
                                           'Block this address from posting?'));
        }
        const mark = body.querySelector('.view-comment.marked');
        if (mark) mark.scrollIntoView({ block: 'center' });
    }

    async function open(pasteId, commentId) {
        try {
            const resp = await fetch('/admin/paste/' + encodeURIComponent(pasteId) + '/data', { credentials: 'same-origin' });
            if (!resp.ok) throw new Error(resp.status === 404 ? 'That paste is gone.' : 'HTTP ' + resp.status);
            render(await resp.json(), commentId || '');
        } catch (e) {
            body.replaceChildren(el('p', '', 'Could not load the paste: ' + e.message));
            actions.replaceChildren();
            document.getElementById('viewTitle').textContent = 'Paste';
            document.getElementById('viewMeta').textContent = '';
        }
        if (!dialog.open) dialog.showModal();
    }

    document.querySelectorAll('.view-paste').forEach(btn =>
        btn.addEventListener('click', () => open(btn.dataset.paste, btn.dataset.comment)));
    document.getElementById('viewClose').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });   // a click on the backdrop
});
