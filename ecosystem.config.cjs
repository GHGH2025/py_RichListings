/** PM2 process config for EC2 production. Use: pm2 start ecosystem.config.cjs */
module.exports = {
  apps: [
    {
      name: "rich-listings",
      script: "run-server.sh",
      interpreter: "/bin/bash",
      cwd: __dirname,
      env: {
        STATUS_PORT: "8000",
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
    },
  ],
};
