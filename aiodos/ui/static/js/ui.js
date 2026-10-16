// Notifications, fenêtres de dialogue et menus contextuels.
import { h, esc } from './dom.js';
import { icon } from './icons.js';

export function toast(message, kind = 'info', ms = 3800) {
  const names = { ok: 'check', err: 'warn', info: 'info' };
  const el = h(`<div class="toast ${kind}">${icon(names[kind] || 'info')}<div>${esc(message)}</div></div>`);
  document.getElementById('toasts').append(el);
  setTimeout(() => el.remove(), ms);
}

export function errorToast(err) {
  toast(err?.message || String(err), 'err', 6000);
}

/**
 * Fenêtre modale. Retourne une promesse { ok, value, checked, choice }.
 * Options : title, text, quote, iconName, confirm, cancel, danger,
 *           check (libellé d'une case), input / textarea (valeur initiale), placeholder,
 *           choices ([{ value, label, help }] : un choix à faire avant de pouvoir confirmer).
 */
export function modal(opts) {
  return new Promise(resolve => {
    const field = opts.textarea !== undefined
      ? `<textarea class="textarea" placeholder="${esc(opts.placeholder || '')}">${esc(opts.textarea)}</textarea>`
      : opts.input !== undefined
        ? `<input class="input" value="${esc(opts.input)}" placeholder="${esc(opts.placeholder || '')}">`
        : '';
    const choices = opts.choices ? `<div class="choices">${opts.choices.map(c => `
      <label class="choice"><input type="radio" name="modal-choice" value="${esc(c.value)}">
        <span><b>${esc(c.label)}</b>${c.help ? `<small>${esc(c.help)}</small>` : ''}</span></label>`).join('')}</div>` : '';
    const back = h(`
      <div class="modal-back" role="dialog" aria-modal="true">
        <div class="modal">
          <h3>${opts.iconName ? icon(opts.iconName) : ''}${esc(opts.title || '')}</h3>
          ${opts.text ? `<div class="text">${esc(opts.text)}</div>` : ''}
          ${opts.code ? `<div class="pair-code">${esc(String(opts.code).replace(/^(\d{3})(\d{3})$/, '$1 $2'))}</div>` : ''}
          ${opts.quote ? `<div class="quote">${esc(opts.quote)}</div>` : ''}
          ${field}
          ${choices}
          ${opts.check ? `<label class="check"><input type="checkbox"> ${esc(opts.check)}</label>` : ''}
          <div class="buttons">
            ${opts.cancel === null ? '' : `<button class="btn ghost" data-act="no">${esc(opts.cancel || 'Annuler')}</button>`}
            <button class="btn ${opts.danger ? 'danger' : 'primary'}" data-act="yes">${esc(opts.confirm || 'OK')}</button>
          </div>
        </div>
      </div>`);
    const input = back.querySelector('.input, .textarea');
    const yes = back.querySelector('[data-act="yes"]');
    if (opts.choices) {               // rien de coché d'avance : le choix est délibéré
      yes.disabled = true;
      back.querySelectorAll('.choice input').forEach(r => { r.onchange = () => { yes.disabled = false; }; });
    }
    const finish = ok => {
      back.remove();
      document.removeEventListener('keydown', onKey, true);
      resolve({ ok, value: input ? input.value : undefined,
                checked: !!back.querySelector('.check input')?.checked,
                choice: back.querySelector('.choice input:checked')?.value });
    };
    const onKey = e => {
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      if (e.key === 'Enter' && !yes.disabled && !(input && input.tagName === 'TEXTAREA')) {
        e.preventDefault();
        finish(true);
      }
    };
    back.addEventListener('click', e => {
      if (e.target === back) finish(false);
      const act = e.target.closest('[data-act]')?.dataset.act;
      if (act) finish(act === 'yes');
    });
    document.addEventListener('keydown', onKey, true);
    document.body.append(back);
    (input || back.querySelector('.choice input') || yes).focus();
    if (input && input.tagName === 'INPUT') input.select();
  });
}

export async function ask(title, label, value = '', placeholder = '') {
  const r = await modal({ title, text: label, input: value, placeholder, confirm: 'Valider' });
  return r.ok ? r.value.trim() : null;
}

export async function confirmDanger(title, text, confirm = 'Supprimer') {
  return (await modal({ title, text, confirm, danger: true, iconName: 'warn' })).ok;
}

let openedMenu = null;

export function closeMenu() {
  openedMenu?.remove();
  openedMenu = null;
}

/** Menu contextuel. items : { label, icon, danger, action } | 'sep' | { title } */
export function menu(anchor, items) {
  closeMenu();
  const el = h('<div class="menu" role="menu"></div>');
  for (const item of items) {
    if (item === 'sep') { el.append(h('<hr>')); continue; }
    if (item.title) { el.append(h(`<div class="menu-title">${esc(item.title)}</div>`)); continue; }
    const btn = h(`<button class="${item.danger ? 'danger' : ''}" role="menuitem">${item.icon ? icon(item.icon) : ''}${esc(item.label)}</button>`);
    btn.addEventListener('click', () => { closeMenu(); item.action(); });
    el.append(btn);
  }
  document.body.append(el);
  const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect()
    : { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y };
  const { width, height } = el.getBoundingClientRect();
  el.style.left = `${Math.min(r.left, innerWidth - width - 8)}px`;
  el.style.top = `${r.bottom + height + 6 > innerHeight ? r.top - height - 6 : r.bottom + 6}px`;
  openedMenu = el;
  setTimeout(() => document.addEventListener('click', closeMenu, { once: true }), 0);
}

window.addEventListener('blur', closeMenu);
window.addEventListener('resize', closeMenu);
