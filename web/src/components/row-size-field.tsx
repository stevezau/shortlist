import { NumberField } from "@/components/number-field";
import { ROW_SIZE_DEFAULT, ROW_SIZE_MAX, ROW_SIZE_MIN } from "@/lib/constants";

/** A free row-size picker: any whole number of titles from {@link ROW_SIZE_MIN} to {@link ROW_SIZE_MAX}. */
export function RowSizeField({
  value,
  onChange,
  label = "How many titles",
  hint = `Any number of titles from ${ROW_SIZE_MIN} to ${ROW_SIZE_MAX}.`,
  presets,
}: {
  value: number;
  onChange: (size: number) => void;
  label?: string;
  hint?: string;
  /** Optional quick choices; the free number field always remains available. */
  presets?: readonly number[];
}) {
  return (
    <NumberField
      value={value}
      onChange={onChange}
      min={ROW_SIZE_MIN}
      max={ROW_SIZE_MAX}
      fallback={ROW_SIZE_DEFAULT}
      unit="titles"
      label={label}
      fallbackLabel={label}
      hint={hint}
      presets={presets}
      presetLabel={(size) => `${size} titles`}
    />
  );
}
