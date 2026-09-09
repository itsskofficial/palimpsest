/**
 * Static export: the whole UI compiles to plain files that the Python backend serves
 * and Electron loads. No Node at runtime, so `pip install palimpsest-notion` ships a
 * working interface with nothing else to run.
 *
 * `assetPrefix: ''` with relative paths matters for the Electron case, where the app is
 * loaded from the local server rather than a domain root.
 */
const nextConfig = {
  output: "export",
  reactStrictMode: true,
  images: { unoptimized: true },   // no image optimiser without a Node server
  trailingSlash: true,             // /settings/ resolves to settings/index.html
};
export default nextConfig;
