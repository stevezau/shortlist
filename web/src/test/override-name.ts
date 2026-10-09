/** The accessible name of a setting's Override / Reset button, from a pattern for the setting's label. */
export const overrideName = (label: RegExp): RegExp => new RegExp(`^(Override|Reset) .*${label.source}`, "i");
