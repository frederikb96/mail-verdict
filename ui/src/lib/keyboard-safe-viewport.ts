/** What the layout needs of `window.visualViewport`. */
export interface ViewportLike {
  height: number
  offsetTop: number
  scale: number
  addEventListener(type: "resize" | "scroll", listener: () => void): void
  removeEventListener(type: "resize" | "scroll", listener: () => void): void
}

export interface WindowLike {
  visualViewport?: ViewportLike | null
  scrollY: number
  scrollTo(x: number, y: number): void
  addEventListener(type: "scroll", listener: () => void): void
  removeEventListener(type: "scroll", listener: () => void): void
}

export const VIEWPORT_HEIGHT_VARIABLE = "--app-vh"

/**
 * Keeps the page as tall as what the visual viewport shows, so a field at the bottom of the page
 * sits right above an on-screen keyboard.
 *
 * iOS does not shrink the layout for its keyboard: Safari pans the page up by the keyboard's
 * height to reveal the focused field, which carries the header out of sight. The variable follows
 * the visual viewport's height, and every pan is undone with a native scroll back to the top.
 * Nothing is positioned or transformed from script, because iOS draws the caret, the selection
 * handles and the edit menu where a field last was and loses them when a layer moves it. A
 * pinch-zoom's pan is left alone.
 *
 * Returns the function that removes the listeners and the variable.
 */
export function attachKeyboardSafeViewport(win: WindowLike, root: HTMLElement): () => void {
  const viewport = win.visualViewport
  if (!viewport) return () => {}

  const sync = () => {
    root.style.setProperty(VIEWPORT_HEIGHT_VARIABLE, `${viewport.height}px`)
    if (viewport.scale !== 1) return
    if (win.scrollY !== 0 || viewport.offsetTop !== 0) win.scrollTo(0, 0)
  }

  sync()
  viewport.addEventListener("resize", sync)
  viewport.addEventListener("scroll", sync)
  win.addEventListener("scroll", sync)
  return () => {
    viewport.removeEventListener("resize", sync)
    viewport.removeEventListener("scroll", sync)
    win.removeEventListener("scroll", sync)
    root.style.removeProperty(VIEWPORT_HEIGHT_VARIABLE)
  }
}
