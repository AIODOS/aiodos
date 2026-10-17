// Typographie française : une espace insécable avant « : ; ? ! » et à l'intérieur des
// guillemets, pour qu'aucun signe ne commence seul une ligne (sur un téléphone surtout).
// Textes de l'interface seulement : messages, code et champs restent tels quels, pour que
// le copier-coller rende exactement ce qui a été écrit.
const SKIP = 'pre, code, textarea, input, .md, .bubble, .mono, .config';
const BEFORE = / ([:;?!»])/g;
const AFTER = /« /g;

function fixText(node) {
  const text = node.data;
  if (!text.includes(' ')) return;
  const fixed = text.replace(BEFORE, ' $1').replace(AFTER, '« ');
  if (fixed !== text) node.data = fixed;
}

function fix(node) {
  if (node.nodeType === Node.TEXT_NODE) {
    if (!node.parentElement?.closest(SKIP)) fixText(node);
    return;
  }
  if (node.nodeType !== Node.ELEMENT_NODE || node.closest(SKIP)) return;
  const walker = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: n => (n.nodeType === Node.TEXT_NODE ? NodeFilter.FILTER_ACCEPT
      : n.matches(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP),
  });
  for (let n = walker.nextNode(); n; n = walker.nextNode()) fixText(n);
}

/** Corrige la page, puis tout ce qui s'y ajoute (vues, menus, fenêtres, notifications). */
export function frenchSpacing(root = document.body) {
  fix(root);
  new MutationObserver(records => {
    for (const record of records) record.addedNodes.forEach(fix);
  }).observe(root, { childList: true, subtree: true });
}
