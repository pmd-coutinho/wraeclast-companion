{
  description = "PoE 1 campaign companion development environment";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs = { nixpkgs, ... }:
    let systems = [ "x86_64-linux" "aarch64-linux" ];
    in {
      devShells = nixpkgs.lib.genAttrs systems (system:
        let pkgs = import nixpkgs { inherit system; };
        in { default = pkgs.mkShell {
        packages = with pkgs; [ python3 python3Packages.pygobject3 python3Packages.pycairo python3Packages.pillow gtk4 gtk4-layer-shell gobject-introspection glib pkg-config ];
        shellHook = ''
          export GI_TYPELIB_PATH="${pkgs.gtk4-layer-shell}/lib/girepository-1.0:${pkgs.gtk4}/lib/girepository-1.0:$GI_TYPELIB_PATH"
          export LD_LIBRARY_PATH="${pkgs.gtk4-layer-shell}/lib:${pkgs.gtk4}/lib:$LD_LIBRARY_PATH"
        '';
      }; });
    };
}
