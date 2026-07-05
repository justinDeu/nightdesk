import clsx, { type ClassValue } from "clsx";

/** Merge class names. Thin wrapper over clsx; the token system is small enough
 *  that Tailwind-merge conflict resolution is not needed yet. */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}
