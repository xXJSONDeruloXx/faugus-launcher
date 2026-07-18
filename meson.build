project(
  'uwu-launcher',
  version: '2.0.0',
  meson_version: '>=1.2.0',
  license: 'MIT',
  license_files: 'LICENSE',
)

fs = import('fs')
gnome = import('gnome')

subdir('assets')
subdir('languages')
subdir('data')
subdir('faugus')

install_data(
  'uwu-launcher',
  install_mode: 'rwxr-xr-x',
  install_dir: get_option('bindir'),
)

install_data(
  'hv/hv_helper.py',
  install_mode: 'rwxr-xr-x',
  install_dir: get_option('libdir') / 'uwu-launcher',
)

install_subdir(
  'hv/cpuid_fault_emulation',
  install_dir: get_option('libdir') / 'uwu-launcher',
)

install_data(
  'data/systemd/uwu-hv-runtime.service',
  install_dir: get_option('libdir') / 'systemd' / 'system',
)

install_data(
  'LICENSE',
  install_dir: get_option('datadir') / 'licenses' / meson.project_name(),
)

gnome.post_install(
  gtk_update_icon_cache: true,
)
