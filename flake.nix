{
  description = "Agent Journal client and server";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
    }:
    let
      inherit (nixpkgs) lib;
      forSystems = lib.genAttrs [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      workspaceOverlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
    in
    {
      packages = forSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonBase = pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; };
          pythonSet = pythonBase.overrideScope (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              workspaceOverlay
            ]
          );
          venv = pythonSet.mkVirtualEnv "agent-journal-runtime" workspace.deps.default;
          inherit (pkgs.callPackages pyproject-nix.build.util { }) mkApplication;
          client = (mkApplication {
            inherit venv;
            package = pythonSet.agent-journal;
          }).overrideAttrs (old: {
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.makeWrapper ];
            postInstall = (old.postInstall or "") + ''
              wrapProgram "$out/bin/agent-journal" \
                --prefix PATH : "${lib.makeBinPath [ pkgs.gitMinimal pkgs.gh ]}" \
                --set-default SSL_CERT_FILE "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
            '';
            meta = (old.meta or { }) // {
              mainProgram = "agent-journal";
            };
          });
        in
        {
          inherit client;
          default = client;
        }
        // lib.optionalAttrs (lib.hasSuffix "-linux" system) { server = client; }
      );

      apps = forSystems (
        system:
        let
          client = {
            type = "app";
            program = "${self.packages.${system}.client}/bin/agent-journal";
            meta.description = "Run the Agent Journal client CLI";
          };
        in
        {
          inherit client;
          default = client;
        }
      );

      checks = forSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          client = pkgs.runCommand "agent-journal-client-check" { } ''
            client="${self.packages.${system}.client}/bin/agent-journal"
            "$client" --help
            "$client" tui --help
            "$client" observe --help
            "$client" mcp --help
            touch "$out"
          '';
        }
        // lib.optionalAttrs (lib.hasSuffix "-linux" system) {
          server = pkgs.runCommand "agent-journal-server-check" { } ''
            server="${self.packages.${system}.server}/bin/agent-journal"
            "$server" serve --help
            "$server" backup --help
            touch "$out"
          '';
        }
      );
    };
}
