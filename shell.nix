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
    echo "=========================================================="
    echo " Xiaomi Bootloader Unlocker (HyperOS) - Ephemeral Nix Env "
    echo "=========================================================="
    echo "To run interactively:"
    echo "  python hyperosunlocker.py"
    echo ""
    echo "To run with token directly:"
    echo "  python hyperosunlocker.py --token <new_bbs_serviceToken>"
    echo "=========================================================="
  '';
}
