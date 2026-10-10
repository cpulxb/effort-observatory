// Keep keyed elements alive so polling does not interrupt keyboard or pointer reading.
const previousMarkup = new WeakMap();

function key(node) {
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  return (
    node.getAttribute("data-key") || node.getAttribute("data-session") || null
  );
}

function compatible(current, next) {
  return (
    current.nodeType === next.nodeType && current.nodeName === next.nodeName
  );
}

function syncNode(current, next) {
  if (current.isEqualNode(next)) return;
  if (current.nodeType !== Node.ELEMENT_NODE) {
    current.nodeValue = next.nodeValue;
    return;
  }
  // The open state belongs to the reader, not the response template.
  const preserveOpen = current.tagName === "DETAILS";
  for (const attribute of [...current.attributes]) {
    if (preserveOpen && attribute.name === "open") continue;
    if (!next.hasAttribute(attribute.name))
      current.removeAttribute(attribute.name);
  }
  for (const attribute of [...next.attributes]) {
    if (preserveOpen && attribute.name === "open") continue;
    if (current.getAttribute(attribute.name) !== attribute.value)
      current.setAttribute(attribute.name, attribute.value);
  }
  syncChildren(current, next);
}

function syncChildren(current, next) {
  const original = [...current.childNodes];
  const keyed = new Map(original.filter(key).map((node) => [key(node), node]));
  const used = new Set();
  let cursor = current.firstChild;
  for (const desired of [...next.childNodes]) {
    const desiredKey = key(desired);
    let existing = desiredKey ? keyed.get(desiredKey) : cursor;
    if (
      existing &&
      (used.has(existing) ||
        key(existing) !== desiredKey ||
        !compatible(existing, desired))
    )
      existing = null;
    if (!existing) {
      existing = desired.cloneNode(true);
      current.insertBefore(existing, cursor);
    } else {
      if (existing !== cursor) current.insertBefore(existing, cursor);
      syncNode(existing, desired);
    }
    used.add(existing);
    cursor = existing.nextSibling;
  }
  for (const node of original) if (!used.has(node)) node.remove();
}

export function hasTextSelection() {
  const selection = window.getSelection();
  return Boolean(selection && !selection.isCollapsed && selection.toString());
}

export function updateMarkup(container, html, preservePosition = true) {
  if (previousMarkup.get(container) === html) return false;
  const anchor = preservePosition
    ? [...container.querySelectorAll("[data-anchor]")].find(
        (node) => node.getBoundingClientRect().bottom > 0,
      )
    : null;
  const anchorTop = anchor?.getBoundingClientRect().top;
  const scrollPositions = [...container.querySelectorAll("*")]
    .filter((node) => node.scrollTop || node.scrollLeft)
    .map((node) => [node, node.scrollTop, node.scrollLeft]);
  const template = document.createElement("template");
  template.innerHTML = html;
  syncChildren(container, template.content);
  for (const [node, top, left] of scrollPositions) {
    if (node.isConnected) {
      node.scrollTop = top;
      node.scrollLeft = left;
    }
  }
  if (anchor?.isConnected && anchorTop < window.innerHeight) {
    const difference = anchor.getBoundingClientRect().top - anchorTop;
    if (Math.abs(difference) > 1) window.scrollBy(0, difference);
  }
  previousMarkup.set(container, html);
  return true;
}
