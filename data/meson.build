install_data(
  'io.github.xXJSONDeruloXx.uwu-launcher.metainfo.xml',
  install_dir: get_option('datadir') / 'metainfo',
)

icon_name = 'io.github.xXJSONDeruloXx.uwu-launcher'

launcher_out = 'io.github.xXJSONDeruloXx.uwu-launcher.desktop'
shortcut_out = 'io.github.xXJSONDeruloXx.uwu-launcher.shortcut.desktop'

launcher_desktop = configure_file(
  input: 'io.github.xXJSONDeruloXx.uwu-launcher.desktop.in',
  output: launcher_out,
  configuration: {
    'ICON': icon_name,
    'EXEC': 'uwu-launcher',
  }
)

shortcut_desktop = configure_file(
  input: 'io.github.xXJSONDeruloXx.uwu-launcher.shortcut.desktop.in',
  output: shortcut_out,
  configuration: {
    'ICON': icon_name,
  }
)

install_data(
  launcher_desktop,
  shortcut_desktop,
  install_dir: get_option('datadir') / 'applications'
)
