const publicPath = process.env.PUBLIC_PATH || '';
module.exports = {
  output: 'export',
  basePath: publicPath || undefined,
  trailingSlash: true,
  images: { unoptimized: true },
};
