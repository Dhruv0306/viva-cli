const { getUser } = require('./models/user');
const { registerRoutes } = require('./routes/handler');

registerRoutes();
getUser(1);
