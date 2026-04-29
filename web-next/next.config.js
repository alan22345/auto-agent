/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  async rewrites() {
    const apiTarget = process.env.API_URL || 'http://localhost:2020';
    return [
      { source: '/api/:path*', destination: `${apiTarget}/api/:path*` },
      { source: '/ws', destination: `${apiTarget}/ws` },
    ];
  },
};
module.exports = nextConfig;
