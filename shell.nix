{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  name = "xiaomi-bootloader-unlocker";

  buildInputs = [
    (pkgs.python3.withPackages (ps: with ps; [
      ntplib
      pytz
      urllib3
      colorama
    ]))
  ];

  shellHook = ''
    export HYPEROS_UNLOCKER_SCRIPT="${toString ./.}/hyperosunlocker.py"
    hyperosunlocker() {
      python "$HYPEROS_UNLOCKER_SCRIPT" "$@"
    }

    echo "=========================================================="
    echo " Xiaomi Bootloader Unlocker (HyperOS) - Ephemeral Nix Env "
    echo "=========================================================="
    echo "To run interactively:"
    echo "  hyperosunlocker"
    echo ""
    echo "To run with token directly:"
    echo "  hyperosunlocker --token <new_bbs_serviceToken>"
    echo "=========================================================="
  '';
}
