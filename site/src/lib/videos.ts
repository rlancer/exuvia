/**
 * exuvia's videos on YouTube (channel @lobstermp), made in `video/` at the
 * root of the repo, and the player the site shows them in.
 *
 * The player is light: a poster and a play button that link to the video on
 * YouTube. A click swaps them for YouTube's player from youtube-nocookie.com
 * (the script in layouts/Base.astro), so a visitor who never presses play
 * loads nothing from YouTube. Without JavaScript the link opens YouTube.
 *
 * Each poster is `src/assets/videos/<name>.jpg`, 1280x720: a frame of the
 * video's own title card, the same picture as its thumbnail on YouTube.
 */
import type { ImageMetadata } from "astro";
import { getImage } from "astro:assets";
import { escapeHtml } from "./highlight";

export type Video = {
  /** The YouTube video ID. */
  id: string;
  title: string;
  /** As YouTube's player shows it. */
  length: string;
};

export const VIDEOS = {
  "exuvia-overview": { id: "umKwE1bLPIU", title: "exuvia: the notebook for the AI era", length: "2:22" },
  "getting-started": { id: "8K_UnMj2qSo", title: "Getting started with exuvia", length: "5:30" },
} satisfies Record<string, Video>;

export type VideoName = keyof typeof VIDEOS;

export function isVideoName(name: string): name is VideoName {
  return name in VIDEOS;
}

export function watchUrl(name: VideoName): string {
  return `https://www.youtube.com/watch?v=${VIDEOS[name].id}`;
}

const posters = import.meta.glob<{ default: ImageMetadata }>("../assets/videos/*.jpg", { eager: true });

/** The player's HTML: the poster, a play button and the title, as a link to the video. */
export async function videoPlayer(name: VideoName, sizes: string): Promise<string> {
  const video = VIDEOS[name];
  const src = posters[`../assets/videos/${name}.jpg`]?.default;
  if (!src) throw new Error(`No poster src/assets/videos/${name}.jpg for the video "${name}".`);
  const image = await getImage({ src, widths: [640, 960, 1280], sizes, format: "webp", quality: 82 });
  const title = escapeHtml(video.title);
  return (
    `<div class="video" data-video="${video.id}" data-title="${title}">` +
    `<a class="video-poster" href="${watchUrl(name)}" rel="noopener" aria-label="Play ${title} (${video.length})">` +
    `<img src="${image.src}" srcset="${image.srcSet.attribute}" sizes="${sizes}" width="${src.width}" height="${src.height}" ` +
    `alt="" loading="lazy" decoding="async">` +
    `<span class="video-play" aria-hidden="true">` +
    `<svg viewBox="0 0 24 24" width="20" height="20"><path d="M7 4.5v15l12.5-7.5z" fill="currentColor"/></svg>` +
    `Play<span class="video-length">${video.length}</span></span>` +
    `</a></div>`
  );
}
