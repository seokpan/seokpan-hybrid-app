// Only explicitly marked, non-sensitive controls can be restored after a command.
// The caller also checks that Cookie recovery returned the same user/participation.
export function captureCommandFocus() {
  const source = document.activeElement;
  const key = source instanceof HTMLElement ? source.dataset.commandFocus : undefined;
  if (!key) return null;
  let moved = false;
  const onFocus = (event: FocusEvent) => {
    if (event.target !== document.body && event.target !== source) moved = true;
  };
  document.addEventListener("focusin", onFocus);
  const cancel = () => document.removeEventListener("focusin", onFocus);
  return {
    cancel,
    restore() {
      cancel();
      if (moved || document.activeElement !== document.body) return;
      const target = Array.from(
        document.querySelectorAll<HTMLElement>("[data-command-focus]"),
      ).find((element) => element.dataset.commandFocus === key);
      if (target && !target.matches(":disabled")) target.focus({ preventScroll: true });
    },
  };
}
