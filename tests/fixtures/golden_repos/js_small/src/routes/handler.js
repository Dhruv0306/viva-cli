const { getUser } = require('../models/user');

function registerRoutes() {
  return getUser;
}

module.exports = { registerRoutes };
