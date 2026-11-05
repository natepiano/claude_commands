{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { nixpkgs, ... }: {
    devShells.x86_64-linux.default = nixpkgs.legacyPackages.x86_64-linux.mkShellNoCC {
      # The proof checks the host tools without another repository's shell.
      shellHook = ''
        export PATH="$HOME/.cargo/bin:/run/current-system/sw/bin:$PATH"
      '';
    };
  };
}
