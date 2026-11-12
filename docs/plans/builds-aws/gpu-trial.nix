# Build this system once on natedev, then import it into the official gate image.
{ runtimeRoots, toolchain }:
{ config, lib, pkgs, modulesPath, ... }:
{
  imports = [ "${modulesPath}/virtualisation/amazon-image.nix" ];
  nixpkgs.hostPlatform = "x86_64-linux";
  nixpkgs.config.allowUnfree = true;
  system.stateVersion = "26.05";
  networking.hostName = "gpu-size-trial";
  time.timeZone = "UTC";
  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  services.xserver.videoDrivers = [ "nvidia" ];
  hardware.graphics.enable = true;
  hardware.nvidia = {
    package = config.boot.kernelPackages.nvidiaPackages.production;
    open = false;
    modesetting.enable = true;
    nvidiaSettings = false;
  };
  environment.systemPackages = with pkgs; [ python3 cargo-nextest vulkan-tools git curl ] ++ [ toolchain ];
  system.extraDependencies = runtimeRoots;
  users.users.gpu-trial = {
    isNormalUser = true;
    extraGroups = [ "video" "render" ];
  };
  systemd.tmpfiles.rules = [ "d /var/lib/gpu-size-trial 0700 gpu-trial users -" ];
  # Phase 2 installs this command before the single reboot.
  systemd.services.gpu-size-trial = {
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    unitConfig.ConditionPathIsExecutable = "/var/lib/gpu-size-trial/run";
    environment.WGPU_BACKEND = "vulkan";
    serviceConfig = {
      Type = "oneshot";
      User = "gpu-trial";
      WorkingDirectory = "/var/lib/gpu-size-trial";
      ExecStart = "/var/lib/gpu-size-trial/run";
      KillMode = "control-group";
    };
  };
  services.openssh.enable = lib.mkForce false;
}
