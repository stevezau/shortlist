// A 16-36px control is a small target for a thumb (Apple and Material ask for 44px). On a touch
// screen a transparent 44px box centred on the control takes the extra taps; it draws nothing, so the
// control looks the same there and on desktop, where none of this applies. Put it on the element
// that owns the click: a button, or the <label> wrapping a native checkbox.
export const coarseHitArea =
  "[@media(pointer:coarse)]:relative [@media(pointer:coarse)]:before:absolute [@media(pointer:coarse)]:before:left-1/2 [@media(pointer:coarse)]:before:top-1/2 [@media(pointer:coarse)]:before:h-11 [@media(pointer:coarse)]:before:w-full [@media(pointer:coarse)]:before:min-w-11 [@media(pointer:coarse)]:before:-translate-x-1/2 [@media(pointer:coarse)]:before:-translate-y-1/2";
