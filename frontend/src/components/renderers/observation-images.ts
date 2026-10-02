export interface ObservationImage {
  index: number;
  src: string;
  image: HTMLImageElement;
}

/** Owns decoded image resources for one recording, including in-flight requests. */
class ObservationImages {
  private abort = new AbortController();
  private pending = new Map<number, Promise<ObservationImage>>();
  private ready = new Map<number, ObservationImage>();
  private pinned = -1;

  constructor(private urls: readonly string[]) {}

  get(index: number) {
    return this.ready.get(index);
  }

  pin(index: number) {
    this.pinned = index;
    const frame = this.ready.get(index);
    if (frame) {
      this.ready.delete(index);
      this.ready.set(index, frame);
    }
  }

  load(index: number): Promise<ObservationImage> {
    const frame = this.ready.get(index);
    if (frame) return Promise.resolve(frame);
    const existing = this.pending.get(index);
    if (existing) return existing;
    const request = this.decode(index).finally(() => this.pending.delete(index));
    this.pending.set(index, request);
    return request;
  }

  prefetch(index: number) {
    for (let next = index + 1; next <= index + 2 && next < this.urls.length; next++) {
      // A failed prefetch can be retried when that frame is actually requested.
      void this.load(next).catch(() => {});
    }
  }

  private async decode(index: number) {
    const response = await fetch(this.urls[index], { signal: this.abort.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    this.abort.signal.throwIfAborted();
    const src = URL.createObjectURL(blob);
    try {
      const image = new Image();
      image.src = src;
      await image.decode();
      this.abort.signal.throwIfAborted();
      const frame = { index, src, image };
      this.ready.set(index, frame);
      for (const [key, old] of this.ready) {
        if (this.ready.size <= 8) break;
        if (key === this.pinned || key === index) continue;
        URL.revokeObjectURL(old.src);
        this.ready.delete(key);
      }
      return frame;
    } catch (error) {
      URL.revokeObjectURL(src);
      throw error;
    }
  }

  dispose() {
    this.abort.abort();
    for (const frame of this.ready.values()) URL.revokeObjectURL(frame.src);
    this.ready.clear();
  }
}

/**
 * Everything one episode viewer downloads, created once per trial attempt and
 * index revision. The recording and the first image start together; disposal
 * cancels both and releases decoded images. Recording bytes are not cached
 * beyond this object.
 */
export class PlaybackResources {
  readonly recording: Promise<Uint8Array>;
  readonly images: ObservationImages | null;
  private abort = new AbortController();

  constructor(recordingUrl: string, imageUrls: readonly string[], firstImage: number) {
    this.recording = fetch(recordingUrl, { signal: this.abort.signal }).then(async response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return new Uint8Array(await response.arrayBuffer());
    });
    // The viewer reports failures when it awaits the recording.
    this.recording.catch(() => {});
    this.images = imageUrls.length ? new ObservationImages(imageUrls) : null;
    void this.images?.load(firstImage).catch(() => {});
  }

  dispose() {
    this.abort.abort();
    this.images?.dispose();
  }
}
