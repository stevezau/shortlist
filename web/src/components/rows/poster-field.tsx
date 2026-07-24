import { useEffect, useRef, useState } from "react";

import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api";
import { useImageProvider } from "@/lib/queries";
import type { PosterInput } from "@/lib/types";

const PLACEHOLDER_HINT = (
  <p className="text-sm text-muted-foreground">
    Use <span className="font-mono">{"{user}"}</span> for each person's name,{" "}
    <span className="font-mono">{"{library_name}"}</span> for the library, or{" "}
    <span className="font-mono">{"{top_seed}"}</span> for a title they recently
    watched.
  </p>
);

/**
 * The row editor's "Poster" section: leave Plex's own artwork alone, upload an image, or generate one
 * from text with the AI provider. The image upload/preview act on a saved row, so for a brand-new row
 * (no id yet) those affordances explain the row must be saved first — the mode + text still save.
 */
export function PosterField({
  value,
  onChange,
  collectionId,
  hasImage,
}: {
  value: PosterInput;
  onChange: (poster: PosterInput) => void;
  collectionId: number | null;
  hasImage: boolean;
}) {
  const provider = useImageProvider();
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploadedNow, setUploadedNow] = useState(false);
  const [imageVersion, setImageVersion] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  useEffect(
    () => () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    },
    [previewUrl],
  );

  const set = (patch: Partial<PosterInput>) => onChange({ ...value, ...patch });
  const showImage = hasImage || uploadedNow;
  const aiCapable = provider.data?.capable ?? true; // assume capable until we know, to avoid a flash
  const isTextMode = value.mode === "text";
  const isAiMode = value.mode === "ai";
  const previewable =
    collectionId !== null && (isTextMode || (isAiMode && aiCapable));

  const onFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file || collectionId === null) return;
    setUploading(true);
    setUploadError(null);
    try {
      await api.uploadPosterImage(collectionId, file);
      set({ mode: "upload" });
      setUploadedNow(true);
      setImageVersion((v) => v + 1);
    } catch (error) {
      setUploadError(apiErrorMessage(error, "Couldn't upload that image."));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = ""; // let the same file be re-picked
    }
  };

  const removeImage = async () => {
    if (collectionId === null) return;
    try {
      await api.deletePosterImage(collectionId);
      setUploadedNow(false);
      setImageVersion((v) => v + 1);
    } catch (error) {
      setUploadError(apiErrorMessage(error, "Couldn't remove that image."));
    }
  };

  const doPreview = async () => {
    if (collectionId === null) return;
    setPreviewing(true);
    setPreviewError(null);
    try {
      const blob = await api.previewPoster(collectionId, value);
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      setPreviewUrl(URL.createObjectURL(blob));
    } catch (error) {
      setPreviewError(apiErrorMessage(error, "Couldn't generate a preview."));
    } finally {
      setPreviewing(false);
    }
  };

  return (
    <div className="space-y-3 border-t pt-4">
      <Label>Poster</Label>
      <p className="text-sm text-muted-foreground">
        The artwork for this row's collection on Plex. Leave it on Plex's own
        artwork, upload your own, or generate one with your AI provider.
      </p>
      <Segmented
        value={value.mode || "none"}
        onChange={(mode) =>
          set({ mode: mode === "none" ? "" : (mode as PosterInput["mode"]) })
        }
        ariaLabel="Poster source"
        options={[
          { value: "none", label: "Plex default" },
          { value: "upload", label: "Upload" },
          { value: "text", label: "Text" },
          { value: "ai", label: "AI image" },
        ]}
      />

      {value.mode === "upload" &&
        (collectionId === null ? (
          <p className="text-sm text-muted-foreground">
            Save the row first, then upload an image here.
          </p>
        ) : (
          <div className="space-y-3">
            {showImage && (
              <img
                src={`${api.posterImageUrl(collectionId)}?v=${imageVersion}`}
                alt="Current row poster"
                className="h-48 w-32 rounded-md border object-cover"
              />
            )}
            <div className="flex items-center gap-2">
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={onFile}
              />
              <Button
                type="button"
                variant="outline"
                loading={uploading}
                onClick={() => fileRef.current?.click()}
              >
                {showImage ? "Replace image" : "Choose image"}
              </Button>
              {showImage && (
                <Button type="button" variant="ghost" onClick={removeImage}>
                  Remove
                </Button>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              A tall poster (2:3) looks best. JPG or PNG, up to 8&nbsp;MB.
            </p>
            {uploadError && (
              <p role="alert" className="text-sm text-destructive">
                {uploadError}
              </p>
            )}
          </div>
        ))}

      {(isTextMode || isAiMode) && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            {isTextMode
              ? "A clean poster with your text over a gradient — built in, no AI needed."
              : "An AI-generated image from your text and style, using your AI provider."}
          </p>
          {isAiMode && provider.data && !provider.data.capable && (
            <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
              {provider.data.reason} You can use a <strong>Text</strong> poster
              instead — it needs no AI provider.
            </p>
          )}
          <div className="space-y-2">
            <Label htmlFor="poster-title">Title text</Label>
            <Input
              id="poster-title"
              value={value.title}
              placeholder="e.g. {user}'s Weekend Picks"
              maxLength={120}
              onChange={(event) => set({ title: event.target.value })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="poster-subtitle">Subtitle (optional)</Label>
            <Input
              id="poster-subtitle"
              value={value.subtitle}
              placeholder="e.g. Hand-picked from {library_name}"
              maxLength={120}
              onChange={(event) => set({ subtitle: event.target.value })}
            />
          </div>
          {PLACEHOLDER_HINT}
          <div className="space-y-2">
            <Label htmlFor="poster-style">Art style</Label>
            <Input
              id="poster-style"
              value={value.style}
              placeholder="e.g. minimalist neon, painterly, retro VHS"
              maxLength={400}
              onChange={(event) => set({ style: event.target.value })}
            />
          </div>
          <div className="flex items-center gap-3">
            <Button
              type="button"
              variant="outline"
              loading={previewing}
              disabled={!previewable}
              onClick={doPreview}
            >
              Preview
            </Button>
            {collectionId === null && (
              <span className="text-sm text-muted-foreground">
                Save the row first to preview.
              </span>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            The image is generated once and reused across runs — it refreshes
            when you change the text or style.
          </p>
          {previewError && (
            <p role="alert" className="text-sm text-destructive">
              {previewError}
            </p>
          )}
          {previewUrl && (
            <img
              src={previewUrl}
              alt="Generated poster preview"
              className="h-48 w-32 rounded-md border object-cover"
            />
          )}
        </div>
      )}
    </div>
  );
}
