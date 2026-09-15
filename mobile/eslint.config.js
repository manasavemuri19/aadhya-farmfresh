// AAD-MOB-020: `npm run lint` had no config and eslint wasn't even a declared
// dependency, so the script was dead — a green CI check that ran nothing.
// eslint-config-expo ships its own recommended rule set (React Native +
// Expo Router aware) as a flat config; this file just wires it up and
// excludes generated/vendor output.
const { defineConfig } = require('eslint/config');
const expoConfig = require('eslint-config-expo/flat');

module.exports = defineConfig([
  expoConfig,
  {
    ignores: ['dist/*', 'node_modules/*', '.expo/*', 'android/*', 'ios/*'],
  },
]);
