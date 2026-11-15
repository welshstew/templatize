## Adding the build server

Add this to `~/.ssh/config`:

    Host buildbox
      HostName 10.20.0.15
      User ci
      Port 2201

Then run `ssh buildbox` and check the fingerprint is
`SHA256:Qm9vZ2xlLWZpbmdlcnByaW50LWV4YW1wbGU`.
