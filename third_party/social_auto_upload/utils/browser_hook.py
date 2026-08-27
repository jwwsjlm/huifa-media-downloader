from conf import LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH

def get_browser_options():
    options = {
        'headless': LOCAL_CHROME_HEADLESS,
        'executable_path': LOCAL_CHROME_PATH,
        'args': [
            '--disable-blink-features=AutomationControlled',
            '--lang=zh-CN',
            '--disable-infobars',
            '--start-maximized',
            '--no-sandbox',
            '--disable-web-security'
        ]
    }
    return options
