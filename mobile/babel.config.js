module.exports = function (api) {
  api.cache(true);
  return {
    // expo-router/babel is deprecated as of SDK 50 — babel-preset-expo now
    // includes its transforms directly. Listing both throws at bundle time.
    presets: [['babel-preset-expo', { jsxImportSource: 'react' }]],
    plugins: [['module-resolver', { alias: { '@': './src' } }]],
  };
};
